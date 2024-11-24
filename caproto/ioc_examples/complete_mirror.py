#!/usr/bin/env python3
import contextvars
from functools import cached_property, partial


from caproto.asyncio.client import Context
from caproto.server import PVGroup, pvproperty, run, template_arg_parser
from caproto import AccessRights

import caproto as ca
import caproto.sync.client as csc


internal_process = contextvars.ContextVar("internal_process", default=False)


class PreloadedContext(Context):
    def __init__(self, *args, cache=None, **kwargs):
        super().__init__(*args, **kwargs)
        cache = cache or {}
        for name, (addr, version) in cache.items():
            self.broadcaster.results.mark_name_found(name, addr)
            self.broadcaster.server_protocol_versions[addr] = version


class MirrorFrame(PVGroup):
    """
    Subscribe to a PV and serve its value.

    The default prefix is ``mirror:``.

    PVs
    ---
    value
    """

    def __init__(self, *args, **kwargs):
        self.pv = None
        self.subscription = None
        self._callbacks = set()
        self._pvs = {}
        self._subs = set()
        super().__init__(*args, **kwargs)

    @cached_property
    def client_context(self):
        return PreloadedContext(cache=self.config)


def make_pvproperty(pv_str, addr_ver, force_read_only):
    addr, ver = addr_ver
    chan = csc.make_channel_from_address(pv_str, addr, 0, 5)
    try:
        # TODO make this public
        resp = csc._read(
            chan,
            1,
            ca.field_types["control"][chan.native_data_type],
            chan.native_data_count,
            notify=True,
            force_int_enums=False,
        )

        if chan.native_data_type in ca.enum_types:
            extra = {
                "enum_strings": tuple(
                    k.decode(chan.string_encoding) for k in resp.metadata.enum_strings
                )
            }
        else:
            extra = {}

        value = pvproperty(
            value=resp.data,
            dtype=chan.native_data_type,
            max_length=chan.native_data_count,
            read_only=force_read_only or (AccessRights.WRITE not in chan.access_rights),
            **extra,
        )

        async def _callback(inst, sub, response):
            # Update our own value based on the monitored one:
            try:
                internal_process.set(True)

                await inst.write(
                    response.data,
                    # We can even make the timestamp the same:
                    timestamp=response.metadata.timestamp,
                )
            finally:
                internal_process.set(False)

        @value.startup
        async def value(self, instance, async_lib):
            # Note that the asyncio context must be created here so that it knows
            # which asyncio loop to use:

            (pv,) = await self.client_context.get_pvs(pv_str)

            # Subscribe to the target PV and register self._callback.
            subscription = pv.subscribe(data_type="time")
            cb = partial(_callback, instance)
            subscription.add_callback(cb)
            self._callbacks.add(cb)
            self._pvs[pv_str] = pv
            self._subs.add(subscription)

        @value.putter
        async def value(self, instance, value):
            if internal_process.get():
                return value
            else:
                pv = self._pvs[pv_str]
                if chan.native_data_type in ca.enum_types:
                    value = instance.get_raw_value(value)

                await pv.write(value, timeout=500)
                # trust the monitor took care of it
                raise ca.SkipWrite()
    finally:
        if chan.states[ca.CLIENT] is ca.CONNECTED:
            csc.send(chan.circuit, chan.clear(), chan.name)

    return value


def make_mirror(config, force_read_only=False):
    try:
        return type(
            "Mirror",
            (MirrorFrame,),
            {
                **{
                    pv_str: make_pvproperty(pv_str, addr_ver, force_read_only)
                    for pv_str, addr_ver in config.items()
                },
                "config": config,
            },
        )
    finally:
        for socket in csc.sockets.values():
            socket.close()

        csc.sockets.clear()
        csc.global_circuits.clear()


if __name__ == "__main__":
    parser, split_args = template_arg_parser(
        default_prefix="mirror:",
        desc="Mirror the value of another floating-point PV.",
        supported_async_libs=("asyncio",),
    )
    parser.add_argument(
        "--host",
        help="ip address of IOC to be mirrored",
        required=True,
        type=str,
    )
    parser.add_argument(
        "--port",
        help="port number of IOC to be mirrored",
        required=True,
        type=int,
    )
    parser.add_argument(
        "--ca-version",
        help="version of the CA protocol the mirrored IOC speaks",
        required=False,
        type=int,
        default=13,
    )
    parser.add_argument("pvs", help="PVs to be mirrored", type=str, nargs="*")

    args = parser.parse_args()
    ioc_options, run_options = split_args(args)

    config = {k: ((args.host, args.port), args.ca_version) for k in args.pvs}
    ioc = make_mirror(config)(**ioc_options)
    run(ioc.pvdb, **run_options)
