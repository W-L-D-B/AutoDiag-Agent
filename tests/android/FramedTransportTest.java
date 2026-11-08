package io.autodiag.agent;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/** JVM-only behavioral checks; does not start the backend or emulator. */
public final class FramedTransportTest {
    private interface Responder { void respond(Socket socket) throws Exception; }
    private interface Client { void invoke(int port) throws Exception; }
    private static int checks;

    private static void require(boolean value, String reason) {
        if (!value) throw new AssertionError(reason);
    }

    private static byte[] readRequest(Socket socket) throws IOException {
        DataInputStream input = new DataInputStream(socket.getInputStream());
        int length = input.readInt();
        require(length > 0 && length <= FramedTransport.MAX_FRAME, "request length");
        byte[] bytes = new byte[length];
        input.readFully(bytes);
        return bytes;
    }

    private static void withServer(Responder responder, Client client) throws Exception {
        ExecutorService worker = Executors.newSingleThreadExecutor();
        try (ServerSocket listener = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) {
            listener.setSoTimeout(3000);
            Future<?> response = worker.submit(() -> {
                try (Socket peer = listener.accept()) {
                    peer.setSoTimeout(3000);
                    responder.respond(peer);
                } catch (Exception error) { throw new RuntimeException(error); }
            });
            client.invoke(listener.getLocalPort());
            response.get(4, TimeUnit.SECONDS);
            checks++;
        } finally { worker.shutdownNow(); }
    }

    private static void fragmentedResponse() throws Exception {
        byte[] request = "诊断 bms".getBytes(StandardCharsets.UTF_8);
        byte[] result = "{\"ok\":true}".getBytes(StandardCharsets.UTF_8);
        withServer(peer -> {
            require(Arrays.equals(request, readRequest(peer)), "UTF-8 request changed");
            DataOutputStream output = new DataOutputStream(peer.getOutputStream());
            output.writeByte(0); output.flush();
            output.writeByte(0); output.flush();
            output.writeShort(result.length); output.flush();
            output.write(result, 0, 3); output.flush();
            output.write(result, 3, result.length - 3); output.flush();
        }, port -> require(Arrays.equals(result, FramedTransport.exchange("127.0.0.1", port, request, 1000)), "response changed"));
    }

    private static void badLength(final int size) throws Exception {
        withServer(peer -> {
            readRequest(peer);
            new DataOutputStream(peer.getOutputStream()).writeInt(size);
        }, port -> {
            try {
                FramedTransport.exchange("127.0.0.1", port, new byte[]{1}, 1000);
                throw new AssertionError("invalid length accepted");
            } catch (IOException expected) { require(!(expected instanceof SocketTimeoutException), "length should fail immediately"); }
        });
    }

    private static void partialEof(final boolean header) throws Exception {
        withServer(peer -> {
            readRequest(peer);
            DataOutputStream output = new DataOutputStream(peer.getOutputStream());
            if (header) output.writeShort(0);
            else { output.writeInt(8); output.write(new byte[]{1, 2, 3}); }
        }, port -> {
            try {
                FramedTransport.exchange("127.0.0.1", port, new byte[]{1}, 1000);
                throw new AssertionError("truncated response accepted");
            } catch (EOFException expected) { }
        });
    }

    private static void absoluteDeadline() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        try {
            withServer(peer -> {
                readRequest(peer);
                DataOutputStream output = new DataOutputStream(peer.getOutputStream());
                output.writeInt(4); output.writeByte(1); output.flush();
                release.await(2, TimeUnit.SECONDS);
            }, port -> {
                long start = System.nanoTime();
                try {
                    FramedTransport.exchange("127.0.0.1", port, new byte[]{1}, 150);
                    throw new AssertionError("missing body did not time out");
                } catch (SocketTimeoutException expected) {
                    require(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start) < 1200, "deadline was extended");
                } finally { release.countDown(); }
            });
        } finally { release.countDown(); }
    }

    private static void outboundValidation() throws Exception {
        for (byte[] request : new byte[][]{new byte[0], new byte[FramedTransport.MAX_FRAME + 1]}) {
            try {
                FramedTransport.exchange("127.0.0.1", 0, request, 1000);
                throw new AssertionError("invalid outbound frame accepted");
            } catch (IOException expected) { checks++; }
        }
    }

    public static void main(String[] ignored) throws Exception {
        fragmentedResponse();
        badLength(0); badLength(FramedTransport.MAX_FRAME + 1); badLength(-1);
        partialEof(true); partialEof(false);
        absoluteDeadline();
        outboundValidation();
        System.out.println("FramedTransport: " + checks + " checks passed");
    }
}
