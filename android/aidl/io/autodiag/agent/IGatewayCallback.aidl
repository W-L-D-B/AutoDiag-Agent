package io.autodiag.agent;

oneway interface IGatewayCallback {
    void onConnectionState(String state);
}
