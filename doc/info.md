# Info service

The info service (defined in [info.proto](../protos/dynod_commons/api/info.proto)) allows to fetch information about available services for this RPC server.


---
## get

**`rpc get (Empty) returns (MultiServiceInfo);`**

#### *Behavior*
List known availalbe services information.

#### *Return*
A **`MultiServiceInfo`** message, including information for each available service:
* module name and version
* current and supported API version
