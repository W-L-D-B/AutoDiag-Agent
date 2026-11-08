package io.autodiag.agent;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Process;
import android.os.RemoteException;
import android.util.Log;
import android.view.View;
import android.view.WindowInsets;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.Locale;
import java.util.NoSuchElementException;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

public final class MainActivity extends Activity {
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final AtomicInteger sequence = new AtomicInteger();
    private final AtomicBoolean snapshotInFlight = new AtomicBoolean();
    private final AtomicBoolean agentPollInFlight = new AtomicBoolean();
    private final ThreadPoolExecutor background = new ThreadPoolExecutor(2, 2, 0,
            TimeUnit.MILLISECONDS, new ArrayBlockingQueue<Runnable>(8),
            runnable -> new Thread(runnable, "autodiag-ui-worker"), new ThreadPoolExecutor.AbortPolicy());
    private volatile IVehicleGateway gateway;
    private boolean active;
    private boolean bound;
    private int bindAttempts;
    private IBinder boundBinder;
    private int servicePid;
    private boolean smokeMode;
    private boolean smokeLogged;
    private boolean automaticQuestionSent;
    private long bindingGeneration;
    private long agentGeneration;
    private String taskId = "";
    private String taskStatus = "";
    private JSONObject lastSnapshot;
    private TextView connectionStatus;
    private TextView pids;
    private TextView signals;
    private TextView diagnostics;
    private TextView agentState;
    private TextView report;
    private EditText question;
    private Spinner nodeSpinner;

    private final IBinder.DeathRecipient deathRecipient = () -> ui.post(() -> {
        if (active) reconnect("网关进程已退出，正在重新绑定");
    });
    private final IGatewayCallback callback = new IGatewayCallback.Stub() {
        @Override public void onConnectionState(String state) {
            ui.post(() -> {
                if (!active) return;
                if ("connected".equals(state)) connectionStatus.setText("网关已连接");
                else if ("disconnected".equals(state)) markUnavailable("网关连接不可用，正在重试");
            });
        }
    };
    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            if (!active) return;
            gateway = IVehicleGateway.Stub.asInterface(binder);
            boundBinder = binder;
            bindingGeneration++;
            bindAttempts = 0;
            try {
                binder.linkToDeath(deathRecipient, 0);
                gateway.registerCallback(callback);
                servicePid = gateway.getProcessId();
                pids.setText("客户端 PID " + Process.myPid() + " · 独立网关 PID " + servicePid);
                connectionStatus.setText("已绑定独立网关，正在读取车辆状态");
                requestSnapshot();
                String automatic = automaticQuestionSent ? null : authorizedAutomaticQuestion(getIntent());
                if (!automaticQuestionSent && automatic != null && !automatic.trim().isEmpty()) {
                    automaticQuestionSent = true;
                    question.setText(automatic);
                    submitQuestion();
                }
            } catch (RemoteException exception) { reconnect("网关绑定中断，正在恢复"); }
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            if (active) reconnect("网关进程断开，正在恢复");
        }
        @Override public void onBindingDied(ComponentName name) {
            if (active) reconnect("网关绑定失效，正在恢复");
        }
        @Override public void onNullBinding(ComponentName name) {
            if (active) reconnect("网关未提供 Binder 接口");
        }
    };
    private final Runnable snapshotPoll = new Runnable() {
        @Override public void run() {
            if (!active) return;
            requestSnapshot();
            ui.postDelayed(this, 2000);
        }
    };
    private final Runnable taskPoll = new Runnable() {
        @Override public void run() {
            if (!active) return;
            if (!taskId.isEmpty() && ("queued".equals(taskStatus) || "running".equals(taskStatus)
                    || "cancelling".equals(taskStatus) || "cancel_requested".equals(taskStatus))) {
                pollTask();
            }
            ui.postDelayed(this, 1000);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        smokeMode = getIntent().getBooleanExtra("smoke", false);
        taskId = getPreferences(MODE_PRIVATE).getString("task_id", "");
        taskStatus = getPreferences(MODE_PRIVATE).getString("task_status", "");
        buildUi();
        if (!taskId.isEmpty()) agentState.setText("已保存任务 " + taskId + "，可刷新或恢复");
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        smokeMode = intent.getBooleanExtra("smoke", smokeMode);
        smokeLogged = false;
        automaticQuestionSent = false;
        String text = gateway == null ? null : authorizedAutomaticQuestion(intent);
        if (text != null && !text.trim().isEmpty() && gateway != null) {
            automaticQuestionSent = true;
            question.setText(text);
            submitQuestion();
        }
        requestSnapshot();
    }

    @Override protected void onStart() {
        super.onStart();
        active = true;
        bindGateway();
        ui.post(snapshotPoll);
        ui.post(taskPoll);
        if (!taskId.isEmpty()) pollTask();
    }

    @Override protected void onStop() {
        active = false;
        agentGeneration++;
        ui.removeCallbacksAndMessages(null);
        releaseBinding();
        super.onStop();
    }

    @Override protected void onDestroy() {
        background.shutdownNow();
        super.onDestroy();
    }

    private void bindGateway() {
        if (!active || bound) return;
        bindAttempts++;
        bound = bindService(new Intent(this, GatewayService.class), connection, Context.BIND_AUTO_CREATE);
        if (!bound) reconnect("无法绑定网关，请点击刷新重试");
    }

    private void releaseBinding() {
        bindingGeneration++;
        IVehicleGateway service = gateway;
        gateway = null;
        if (service != null) {
            try { service.unregisterCallback(callback); } catch (RemoteException ignored) { }
        }
        if (boundBinder != null) {
            try { boundBinder.unlinkToDeath(deathRecipient, 0); }
            catch (NoSuchElementException ignored) { }
            boundBinder = null;
        }
        if (bound) {
            try { unbindService(connection); } catch (IllegalArgumentException ignored) { }
            bound = false;
        }
    }

    private void reconnect(String message) {
        markUnavailable(message);
        releaseBinding();
        if (active && bindAttempts < 4) ui.postDelayed(this::bindGateway, 500L * (bindAttempts + 1));
    }

    private void markUnavailable(String message) {
        connectionStatus.setText(message);
        if (lastSnapshot != null) showSnapshot(lastSnapshot, false);
    }

    private boolean execute(Runnable command) {
        try { background.execute(command); return true; }
        catch (RejectedExecutionException busy) {
            connectionStatus.setText("请求较多，请稍后重试");
            return false;
        }
    }

    private JSONObject request(String method, JSONObject params) throws JSONException {
        return new JSONObject().put("version", 1).put("id", "android-" + sequence.incrementAndGet())
                .put("method", method).put("params", params);
    }

    private void requestSnapshot() {
        if (gateway == null || !snapshotInFlight.compareAndSet(false, true)) return;
        final long generation = bindingGeneration;
        boolean queued = execute(() -> {
            try {
                IVehicleGateway service = gateway;
                if (service == null) throw new RemoteException("No gateway");
                JSONObject response = new JSONObject(service.rpc(request("snapshot", new JSONObject()).toString()));
                ui.post(() -> {
                    if (!active || generation != bindingGeneration) return;
                    if (response.optBoolean("ok", false) && response.optJSONObject("result") != null) {
                        lastSnapshot = response.optJSONObject("result");
                        showSnapshot(lastSnapshot, true);
                        connectionStatus.setText("网关已连接 · 每 2 秒刷新");
                    } else markUnavailable("读取状态失败：" + describeError(response));
                    logSmoke(response);
                });
            } catch (RemoteException exception) {
                ui.post(() -> { if (active) reconnect("Binder 已断开，正在重新绑定"); });
            } catch (JSONException exception) {
                ui.post(() -> { if (active) markUnavailable("网关返回了无效 JSON"); });
            } finally { snapshotInFlight.set(false); }
        });
        if (!queued) snapshotInFlight.set(false);
    }

    private void showSnapshot(JSONObject value, boolean current) {
        JSONObject nodes = value.optJSONObject("nodes");
        if (nodes == null) { signals.setText("尚未收到节点状态"); return; }
        StringBuilder text = new StringBuilder();
        String[] names = {"bms", "hvac", "body"};
        for (String name : names) {
            JSONObject node = nodes.optJSONObject(name);
            if (node == null) continue;
            text.append(name.toUpperCase(Locale.ROOT)).append("  ");
            if (!current) text.append("状态未知 · 以下为旧值");
            else text.append(node.optBoolean("online") ? "在线" : "离线").append(" · ")
                    .append(dataStatus(node.optString("data_status")));
            text.append('\n');
            JSONObject data = node.optJSONObject("signals");
            if (data == null) text.append("尚未收到有效数据\n");
            else if ("bms".equals(name)) {
                text.append("电压 ").append(number(data, "voltage_v")).append(" V · 电流 ")
                        .append(number(data, "current_a")).append(" A\n温度 ")
                        .append(number(data, "temperature_c")).append(" °C\n");
            } else if ("hvac".equals(name)) {
                boolean valid = data.optBoolean("cabin_sensor_valid");
                text.append(valid ? "车内温度 " : "上次好温度 ").append(number(data, "cabin_temperature_c"))
                        .append(" °C").append(valid ? "" : "（传感器输入无效）").append('\n')
                        .append("设定 ").append(number(data, "target_temperature_c"))
                        .append(" °C · 风量 ").append(number(data, "fan_level")).append('\n');
            } else {
                text.append("车速 ").append(number(data, "speed_kph")).append(" km/h · 供电 ")
                        .append(number(data, "supply_voltage_v")).append(" V\n门状态 ")
                        .append(number(data, "door_mask")).append(" · 灯状态 ")
                        .append(number(data, "light_mask")).append('\n');
            }
            text.append("数据年龄 ").append(node.isNull("data_age_ms") ? "—" : node.opt("data_age_ms"))
                    .append(" ms · 拒绝计数 ").append(node.opt("rejections")).append("\n\n");
        }
        text.append("证据 ").append(value.optString("evidence_id", "未提供"));
        signals.setText(text.toString());
    }

    private void diagnostic(boolean dtc) {
        final String node = nodeSpinner.getSelectedItem().toString();
        final long generation = bindingGeneration;
        execute(() -> {
            try {
                JSONObject params = new JSONObject().put("node", node);
                if (dtc) params.put("status_mask", 255);
                else params.put("did", "bms".equals(node) ? 0xD102 : "hvac".equals(node) ? 0xD200 : 0xD300);
                IVehicleGateway service = gateway;
                if (service == null) throw new RemoteException("No gateway");
                JSONObject response = new JSONObject(service.rpc(request(dtc ? "read_dtcs" : "read_did", params).toString()));
                String rendered = response.optBoolean("ok") ? response.getJSONObject("result").toString(2)
                        : "诊断请求失败：" + describeError(response);
                ui.post(() -> { if (active && generation == bindingGeneration) diagnostics.setText(rendered); });
            } catch (RemoteException exception) {
                ui.post(() -> { if (active) diagnostics.setText("网关未绑定或 Binder 已断开"); });
            } catch (JSONException exception) {
                ui.post(() -> { if (active) diagnostics.setText("诊断响应格式错误"); });
            }
        });
    }

    private void submitQuestion() {
        String text = question.getText().toString().trim();
        if (text.isEmpty() || text.length() > 2000) {
            agentState.setText("请输入 1—2000 字的诊断问题"); return;
        }
        try { agentCall("POST", "/tasks", new JSONObject().put("question", text).toString()); }
        catch (JSONException impossible) { agentState.setText("无法构造任务"); }
    }

    private void taskAction(String action) {
        if (taskId.isEmpty()) { agentState.setText("还没有诊断任务"); return; }
        agentCall("POST", "/tasks/" + taskId + "/" + action, "{}");
    }

    private void pollTask() {
        if (gateway == null || taskId.isEmpty() || !agentPollInFlight.compareAndSet(false, true)) return;
        if (!agentCall("GET", "/tasks/" + taskId, "")) agentPollInFlight.set(false);
    }

    private boolean agentCall(String method, String path, String body) {
        final boolean submission = "POST".equals(method) && "/tasks".equals(path);
        final boolean polling = "GET".equals(method) && path.startsWith("/tasks/");
        final long generation = agentGeneration + (submission ? 1 : 0);
        boolean queued = execute(() -> {
            try {
                IVehicleGateway service = gateway;
                if (service == null) throw new RemoteException("No gateway");
                JSONObject value = new JSONObject(service.agent(method, path, body));
                ui.post(() -> { if (active && generation == agentGeneration) showTask(value); });
            } catch (RemoteException exception) {
                ui.post(() -> { if (active && generation == agentGeneration) agentState.setText("网关未绑定，任务状态未刷新"); });
            } catch (JSONException exception) {
                ui.post(() -> { if (active && generation == agentGeneration) agentState.setText("Agent 响应格式错误"); });
            } finally { if (polling) agentPollInFlight.set(false); }
        });
        if (queued && submission) agentGeneration = generation;
        return queued;
    }

    /** The exported launcher must not let another app silently create paid model jobs. */
    private String authorizedAutomaticQuestion(Intent intent) {
        String text = intent.getStringExtra("agent_question");
        String nonce = intent.getStringExtra("qa_token");
        if (text == null || text.trim().isEmpty() || text.length() > 2000
                || nonce == null || !nonce.matches("[A-Za-z0-9_-]{16,128}")) return null;
        File capability = new File(getFilesDir(), "qa-token");
        if (!capability.isFile() || capability.length() > 128) return null;
        try (FileInputStream input = new FileInputStream(capability)) {
            byte[] value = new byte[(int) capability.length()];
            int count = 0;
            while (count < value.length) {
                int read = input.read(value, count, value.length - count);
                if (read < 0) return null;
                count += read;
            }
            if (!MessageDigest.isEqual(value, nonce.getBytes(StandardCharsets.UTF_8))
                    || !capability.delete()) return null;
            return text;
        } catch (IOException ignored) { return null; }
    }

    private void showTask(JSONObject task) {
        if (task.optJSONObject("error") != null && task.optString("task_id", "").isEmpty()) {
            agentState.setText("Agent 请求失败：" + describeError(task)); return;
        }
        String returnedId = task.optString("task_id", taskId);
        if (!returnedId.matches("[A-Za-z0-9_-]{1,64}")) {
            agentState.setText("任务 ID 格式错误"); return;
        }
        taskId = returnedId;
        taskStatus = task.optString("status", taskStatus);
        getPreferences(MODE_PRIVATE).edit().putString("task_id", taskId).putString("task_status", taskStatus).apply();
        agentState.setText("任务 " + taskId + "\n状态：" + taskStatus);
        StringBuilder text = new StringBuilder();
        JSONArray steps = task.optJSONArray("steps");
        if (steps != null) for (int index = Math.max(0, steps.length() - 20); index < steps.length(); index++) {
            JSONObject step = steps.optJSONObject(index);
            if (step == null) continue;
            text.append(index + 1).append(". ").append(step.optString("tool", "步骤"))
                    .append("  ").append(step.optString("summary", "")).append('\n');
        }
        JSONObject result = task.optJSONObject("report");
        if (result != null) {
            text.append("\n诊断报告\n").append(result.optString("summary", ""));
            try { text.append('\n').append(result.toString(2)); } catch (JSONException ignored) { }
        } else if (task.optJSONObject("error") != null) {
            text.append("\n任务未完成：").append(describeError(task));
        } else text.append("\n正在等待可核验的诊断结果。");
        report.setText(text.toString());
    }

    private void logSmoke(JSONObject response) {
        if (!smokeMode || smokeLogged) return;
        smokeLogged = true;
        try {
            JSONObject result = response.optJSONObject("result");
            JSONObject smoke = new JSONObject().put("ui_pid", Process.myPid()).put("service_pid", servicePid)
                    .put("binder_cross_process", servicePid > 0 && servicePid != Process.myPid())
                    .put("rpc_ok", response.optBoolean("ok", false))
                    .put("request_id", response.optString("id", ""))
                    .put("nodes_count", result != null && result.optJSONObject("nodes") != null ? result.optJSONObject("nodes").length() : 0);
            if (!response.optBoolean("ok")) smoke.put("error", describeError(response));
            Log.i("AutoDiagSmoke", smoke.toString());
        } catch (JSONException impossible) { Log.e("AutoDiagSmoke", "Invalid smoke result"); }
    }

    private static String describeError(JSONObject value) {
        JSONObject error = value.optJSONObject("error");
        if (error == null) return "未知响应";
        return error.optString("code", "error") + ": " + error.optString("message", "请求失败");
    }

    private static String dataStatus(String status) {
        if ("fresh".equals(status)) return "数据新鲜";
        if ("stale".equals(status)) return "数据过期（旧值）";
        return "尚无有效数据";
    }

    private static String number(JSONObject object, String name) {
        Object value = object.opt(name);
        if (!(value instanceof Number)) return "—";
        return String.format(Locale.ROOT, "%.2f", ((Number) value).doubleValue());
    }

    private int dp(int size) { return (int) (size * getResources().getDisplayMetrics().density + 0.5f); }
    private TextView text(LinearLayout parent, String value, int size) {
        TextView view = new TextView(this);
        view.setText(value); view.setTextSize(size); view.setTextColor(Color.rgb(25, 36, 50));
        view.setTextIsSelectable(true); view.setPadding(0, dp(6), 0, dp(6));
        parent.addView(view, new LinearLayout.LayoutParams(-1, -2));
        return view;
    }
    private Button button(LinearLayout parent, String label, int id, Runnable action) {
        Button view = new Button(this);
        view.setText(label); view.setId(id); view.setContentDescription(label);
        view.setOnClickListener(ignored -> action.run());
        parent.addView(view, new LinearLayout.LayoutParams(-1, -2));
        return view;
    }

    private void buildUi() {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.rgb(246, 248, 252));
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        scroll.addView(body, new ScrollView.LayoutParams(-1, -2));
        body.setPadding(dp(18), dp(12), dp(18), dp(24));
        body.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(dp(18), dp(12) + insets.getSystemWindowInsetTop(), dp(18), dp(24) + insets.getSystemWindowInsetBottom());
            return insets;
        });
        text(body, "AutoDiag 车载诊断", 26);
        text(body, "软件台架 · 实时状态与诊断证据", 14);
        connectionStatus = text(body, "正在绑定独立网关", 16); connectionStatus.setId(R.id.connection_status);
        button(body, "刷新实时信号", R.id.refresh_button, () -> {
            if (gateway == null) { bindAttempts = 0; bindGateway(); }
            requestSnapshot();
        });
        signals = text(body, "等待车辆状态；尚不显示默认正常值。", 15); signals.setId(R.id.signals_text);
        text(body, "ECU 诊断", 21);
        nodeSpinner = new Spinner(this); nodeSpinner.setId(R.id.node_spinner);
        nodeSpinner.setAdapter(new ArrayAdapter<String>(this, android.R.layout.simple_spinner_dropdown_item, new String[]{"bms", "hvac", "body"}));
        body.addView(nodeSpinner, new LinearLayout.LayoutParams(-1, -2));
        button(body, "读取诊断信号 DID", R.id.diagnostic_button, () -> diagnostic(false));
        button(body, "读取故障码 DTC", R.id.dtc_button, () -> diagnostic(true));
        diagnostics = text(body, "选择节点后读取真实诊断响应。", 14); diagnostics.setId(R.id.diagnostic_text);
        text(body, "Agent 诊断任务", 21);
        question = new EditText(this); question.setId(R.id.question_edit);
        question.setHint("例如：BMS 温度为什么异常？请取证后解释。"); question.setMinLines(2); question.setTextSize(16);
        body.addView(question, new LinearLayout.LayoutParams(-1, -2));
        button(body, "提交诊断问题", R.id.submit_agent_button, this::submitQuestion);
        button(body, "取消当前任务", R.id.cancel_agent_button, () -> taskAction("cancel"));
        button(body, "刷新或恢复任务", R.id.resume_agent_button, () -> {
            if ("interrupted".equals(taskStatus) || "failed".equals(taskStatus)) taskAction("resume");
            else pollTask();
        });
        agentState = text(body, "尚未提交任务", 15); agentState.setId(R.id.agent_state);
        report = text(body, "任务会显示工具步骤、证据引用和最终报告。", 14); report.setId(R.id.agent_report);
        pids = text(body, "尚未绑定网关进程", 12); pids.setId(R.id.binder_pids);
        setContentView(scroll);
        body.requestApplyInsets();
    }
}
