package io.autodiag.agent;

import io.autodiag.agent.IGatewayCallback;

interface IVehicleGateway {
    String rpc(String requestJson);
    String agent(String method, String path, String body);
    int getProcessId();
    void registerCallback(IGatewayCallback callback);
    void unregisterCallback(IGatewayCallback callback);
}
