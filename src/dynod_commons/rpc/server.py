import inspect
import logging
import traceback
from concurrent import futures
from dataclasses import dataclass
from types import ModuleType
from typing import Callable, List, NoReturn

from google.protobuf.internal.enum_type_wrapper import EnumTypeWrapper
from grpc import server

import dynod_commons
from dynod_commons.api import Empty, InfoApiVersion, MultiServiceInfo, Result, ResultCode, ServiceInfo
from dynod_commons.api.info_pb2_grpc import InfoServiceServicer, add_InfoServiceServicer_to_server
from dynod_commons.rpc.trace import trace_buffer, trace_rpc
from dynod_commons.utils import DynodError

LOG = logging.getLogger(__name__)


class RpcMethod:
    def __init__(self, name: str, manager: object, return_type: object, info: ServiceInfo):
        self.manager_method = getattr(manager, name)
        self.return_type = return_type
        self.info = info

    def __call__(self, request, context):
        LOG.debug(trace_rpc(True, request, context=context))

        try:
            # Verify API version
            metadata = {k: v for k, v in context.invocation_metadata()}
            if "api_version" in metadata:
                client_version = int(metadata["api_version"])
                if client_version > self.info.current_api_version:
                    raise DynodError(
                        f"Server current API version ({self.info.current_api_version}) is too old for client API version ({client_version})",
                        rc=ResultCode.ERROR_API_SERVER_TOO_OLD,
                    )
                elif client_version < self.info.supported_api_version:
                    raise DynodError(
                        f"Client API version ({client_version}) is too old for server supported API version ({self.info.current_api_version})",
                        rc=ResultCode.ERROR_API_CLIENT_TOO_OLD,
                    )

            # Ok, delegate to manager
            result = self.manager_method(request)

        except Exception as e:
            # Something happened during the RPC execution

            # Extract RC if this was a known error
            rc = e.rc if isinstance(e, DynodError) else ResultCode.ERROR

            # Special case if operation just returns a Result object
            r = Result(code=rc, msg=str(e), stack="".join(traceback.format_tb(e.__traceback__)))
            result = r if self.return_type == Result else self.return_type(r=r)

        LOG.debug(trace_rpc(False, result, context=context))
        return result


class RpcServicer:
    """
    Generic servicer implementation, that:
     * logs method calls
     * checks api version
     * routes method to provided manager
    """

    def __init__(self, manager: object, info: ServiceInfo):
        # Filter on manager methods
        for n in filter(lambda x: not x.startswith("_") and callable(getattr(manager, x)), dir(manager)):
            method = getattr(manager, n)
            sig = inspect.signature(method)
            return_type = sig.return_annotation
            # Only methods with declared return type + one input parameter
            if return_type != inspect._empty and len(sig.parameters) == 1:
                LOG.debug(f" >> add method {n} (returns {return_type.__name__})")
                setattr(self, n, RpcMethod(n, manager, return_type, info))
            # Or methods coming from the parent stub
            elif return_type == inspect._empty and len(sig.parameters) == 2 and all(p in ["request", "context"] for p in sig.parameters.keys()):
                LOG.debug(f" >> add stub method {n}")
                setattr(self, n, method)


@dataclass
class RpcServiceDescriptor:
    """
    Data class describing a service to be served by the RpcServer class

    Attributes:
        module: python module providing the service
        api_version: enum class holding the supported/current API versions
        manager: object instance to which delegating services requests
        register_method: method to be called for service registration on the server instance
    """

    module: ModuleType
    api_version: EnumTypeWrapper
    manager: object
    register_method: Callable[[object, object], NoReturn]


class RpcServer(InfoServiceServicer):
    """
    Wrapper to GRPC api to setup an RPC server.

    Arguments:
        port:
            TCP port to be used by the RPC server.
        descriptors:
            list of service RpcServiceDescriptor instances.
    """

    def __init__(self, port: int, descriptors: List[RpcServiceDescriptor]):
        self.__port = port
        LOG.debug(f"Starting RPC server on port {self.__port}")

        # Create server instance
        # TODO: add configuration item for parallel workers
        self.__server = server(futures.ThreadPoolExecutor(max_workers=30))

        # To be able to answer to "get info" rpc
        all_descriptors = [RpcServiceDescriptor(dynod_commons, InfoApiVersion, self, add_InfoServiceServicer_to_server)]

        # Get servicers
        all_descriptors.extend(descriptors)

        # Register everything
        self.__info = []
        for descriptor in all_descriptors:
            # Build info for service
            versions = list(descriptor.api_version.values())
            versions.remove(0)
            info = ServiceInfo(
                name=descriptor.module.__title__, version=descriptor.module.__version__, current_api_version=max(versions), supported_api_version=min(versions)
            )
            LOG.debug(f"Registering service in RPC server: {descriptor.manager.__class__.__name__} -- {trace_buffer(info)}")

            # Remember info
            self.__info.append(info)

            # Register servicer in RPC server
            descriptor.register_method(RpcServicer(descriptor.manager, info), self.__server)

        # Setup port and start
        try:
            self.__server.add_insecure_port(f"[::]:{self.__port}")
        except Exception as e:
            msg = f"Failed to start RPC server on port {self.__port}: {e}"
            LOG.error(msg)
            raise DynodError(msg, rc=ResultCode.ERROR_SUBPROCESS_FAILED)
        self.__server.start()
        LOG.debug(f"RPC server started on port {self.__port}")

    def shutdown(self):
        """
        Shuts down this RPC server instance
        """

        # Stop server and wait for termination
        LOG.debug(f"Shutting down RPC server on port {self.__port}")
        self.__server.stop(None)
        self.__server.wait_for_termination()
        LOG.debug(f"RPC server shut down on port {self.__port}")

    def get(self, request: Empty) -> MultiServiceInfo:
        # Just build message from stored info
        return MultiServiceInfo(items=self.__info)
