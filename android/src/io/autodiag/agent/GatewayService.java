package io.autodiag.agent;

import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import android.os.Process;
import android.os.RemoteCallbackList;
import android.os.RemoteException;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/** Non-exported AIDL service. It alone owns client network requests. */
public final class GatewayService extends Service {
    private static final String HOST = "10.0.2.2";
    private static final Pattern REQUEST_ID = Pattern.compile("[A-Za-z0-9._:-]{1,64}");
    private static final Pattern TASK_PATH = Pattern.compile("/tasks/[A-Za-z0-9_-]{1,64}");
    private static final Pattern TASK_ACTION = Pattern.compile("/tasks/[A-Za-z0-9_-]{1,64}/(cancel|resume)");
    private static final Set<String> METHODS = new HashSet<>(Arrays.asList(
            "health", "snapshot", "list_ecus", "read_did", "read_dtcs", "signal_window", "probe", "contract"));
    private final Semaphore inFlight = new Semaphore(4);
    private final RemoteCallbackList<IGatewayCallback> callbacks = new RemoteCallbackList<>();
    private final Object callbackLock = new Object();

    private final IVehicleGateway.Stub binder = new IVehicleGateway.Stub() {
        @Override public String rpc(String requestJson) {
            String requestId = "";
            if (!inFlight.tryAcquire()) {
                return error(requestId, "busy", "Gateway is busy", true);
            }
            try {
                if (requestJson == null || requestJson.getBytes(StandardCharsets.UTF_8).length > FramedTransport.MAX_FRAME) {
                    throw new JSONException("Request too large");
                }
                JSONObject request = new JSONObject(requestJson);
                Object version = request.opt("version");
                requestId = request.optString("id", "");
                if (!(version instanceof Integer) || ((Integer) version) != 1
                        || !REQUEST_ID.matcher(requestId).matches()
                        || !METHODS.contains(request.optString("method", ""))
                        || !(request.opt("params") instanceof JSONObject) || request.length() != 4) {
                    throw new JSONException("Invalid RPC envelope");
                }
                byte[] wire = FramedTransport.exchange(HOST, 18770,
                        requestJson.getBytes(StandardCharsets.UTF_8), 2500);
                String text = decodeUtf8(wire);
                JSONObject response = new JSONObject(text);
                validateResponse(response, requestId);
                notifyConnection("connected");
                return text;
            } catch (SocketTimeoutException exception) {
                notifyConnection("disconnected");
                return error(requestId, "transport_timeout", "Gateway request timed out", true);
            } catch (IOException exception) {
                android.util.Log.w("AutoDiagGateway", "RPC transport: " + exception.getClass().getSimpleName());
                notifyConnection("disconnected");
                return error(requestId, "unavailable", "Cannot read the vehicle gateway", true);
            } catch (JSONException exception) {
                return error(requestId, "protocol_error", "Invalid gateway JSON envelope", false);
            } finally {
                inFlight.release();
            }
        }

        @Override public String agent(String method, String path, String body) {
            if (!inFlight.tryAcquire()) {
                return error("", "busy", "Gateway is busy", true);
            }
            try {
                boolean validGet = "GET".equals(method)
                        && ("/health".equals(path) || TASK_PATH.matcher(path == null ? "" : path).matches());
                boolean validPost = "POST".equals(method)
                        && ("/tasks".equals(path) || TASK_ACTION.matcher(path == null ? "" : path).matches());
                if ((!validGet && !validPost) || body == null
                        || body.getBytes(StandardCharsets.UTF_8).length > 8192) {
                    return error("", "invalid_request", "Unsupported Agent request", false);
                }
                if (validPost) {
                    new JSONObject(body);
                }
                return http(method, path, body);
            } catch (SocketTimeoutException exception) {
                return error("", "transport_timeout", "Agent request timed out", true);
            } catch (IOException exception) {
                return error("", "unavailable", "Cannot connect to Agent service", true);
            } catch (JSONException exception) {
                return error("", "protocol_error", "Invalid Agent JSON response", false);
            } finally {
                inFlight.release();
            }
        }

        @Override public int getProcessId() { return Process.myPid(); }
        @Override public void registerCallback(IGatewayCallback callback) {
            if (callback != null) callbacks.register(callback);
        }
        @Override public void unregisterCallback(IGatewayCallback callback) {
            if (callback != null) callbacks.unregister(callback);
        }
    };

    private static void validateResponse(JSONObject value, String requestId) throws JSONException {
        Object version = value.opt("version");
        Object ok = value.opt("ok");
        if (!(version instanceof Integer) || ((Integer) version) != 1
                || !requestId.equals(value.opt("id")) || !(ok instanceof Boolean)
                || !value.has("result") || !value.has("error") || value.length() != 5) {
            throw new JSONException("Response identity mismatch");
        }
        if ((Boolean) ok) {
            if (!(value.opt("result") instanceof JSONObject) || !value.isNull("error")) {
                throw new JSONException("Invalid success branch");
            }
        } else {
            if (!value.isNull("result") || !(value.opt("error") instanceof JSONObject)) {
                throw new JSONException("Invalid error branch");
            }
            JSONObject error = value.getJSONObject("error");
            if (error.length() != 4 || !(error.opt("code") instanceof String)
                    || !(error.opt("message") instanceof String)
                    || !(error.opt("retryable") instanceof Boolean)
                    || !(error.opt("details") instanceof JSONObject)) {
                throw new JSONException("Invalid error metadata");
            }
        }
    }

    private static String decodeUtf8(byte[] data) throws CharacterCodingException {
        return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(data)).toString();
    }

    private static String http(String method, String path, String body) throws IOException, JSONException {
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(4000);
        HttpURLConnection connection = (HttpURLConnection) new URL("http://" + HOST + ":18771" + path).openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(1500);
            connection.setReadTimeout(2000);
            connection.setRequestMethod(method);
            connection.setRequestProperty("Accept", "application/json");
            if ("POST".equals(method)) {
                byte[] payload = body.getBytes(StandardCharsets.UTF_8);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setDoOutput(true);
                connection.setFixedLengthStreamingMode(payload.length);
                try (OutputStream stream = connection.getOutputStream()) { stream.write(payload); }
            }
            int status = connection.getResponseCode();
            if (status >= 300 && status < 400) throw new IOException("Redirect not allowed");
            InputStream input = status < 400 ? connection.getInputStream() : connection.getErrorStream();
            if (input == null) throw new IOException("Missing HTTP response");
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            try (InputStream stream = input) {
                byte[] buffer = new byte[2048];
                int count;
                while (true) {
                    long remaining = deadline - System.nanoTime();
                    if (remaining <= 0) throw new SocketTimeoutException("Agent deadline exceeded");
                    connection.setReadTimeout((int) Math.max(1, TimeUnit.NANOSECONDS.toMillis(remaining)));
                    count = stream.read(buffer);
                    if (count < 0) break;
                    if (bytes.size() + count > FramedTransport.MAX_FRAME) throw new IOException("Agent response too large");
                    bytes.write(buffer, 0, count);
                }
            }
            String text = decodeUtf8(bytes.toByteArray());
            new JSONObject(text);
            return text;
        } finally {
            connection.disconnect();
        }
    }

    private static String error(String id, String code, String message, boolean retryable) {
        try {
            return new JSONObject().put("version", 1).put("id", id).put("ok", false)
                    .put("result", JSONObject.NULL)
                    .put("error", new JSONObject().put("code", code).put("message", message)
                            .put("retryable", retryable).put("details", new JSONObject())).toString();
        } catch (JSONException impossible) {
            throw new IllegalStateException("Cannot construct error envelope", impossible);
        }
    }

    private void notifyConnection(String state) {
        synchronized (callbackLock) {
            int count = callbacks.beginBroadcast();
            try {
                for (int index = 0; index < count; index++) {
                    try { callbacks.getBroadcastItem(index).onConnectionState(state); }
                    catch (RemoteException ignored) { /* Dead clients are removed by RemoteCallbackList. */ }
                }
            } finally { callbacks.finishBroadcast(); }
        }
    }

    @Override public IBinder onBind(Intent intent) { return binder; }
    @Override public void onDestroy() {
        synchronized (callbackLock) { callbacks.kill(); }
        super.onDestroy();
    }
}
