package io.autodiag.agent;

import java.io.EOFException;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.SocketTimeoutException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.channels.SocketChannel;
import java.util.concurrent.TimeUnit;

/** Socket operations use one absolute deadline, including fragmented input. */
public final class FramedTransport {
    public static final int MAX_FRAME = 65536;

    private FramedTransport() {}

    public static byte[] exchange(String host, int port, byte[] request, int timeoutMs)
            throws IOException {
        if (request.length == 0 || request.length > MAX_FRAME || timeoutMs <= 0) {
            throw new IOException("Invalid framed request size or timeout");
        }
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs);
        try (SocketChannel channel = SocketChannel.open(); Selector selector = Selector.open()) {
            channel.configureBlocking(false);
            SelectionKey key = channel.register(selector, SelectionKey.OP_CONNECT);
            if (!channel.connect(new InetSocketAddress(host, port))) {
                while (!channel.finishConnect()) {
                    await(selector, key, SelectionKey.OP_CONNECT, deadline);
                }
            }
            ByteBuffer output = ByteBuffer.allocate(4 + request.length).order(ByteOrder.BIG_ENDIAN);
            output.putInt(request.length).put(request).flip();
            while (output.hasRemaining()) {
                checkDeadline(deadline);
                if (channel.write(output) == 0) {
                    await(selector, key, SelectionKey.OP_WRITE, deadline);
                }
            }
            ByteBuffer header = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN);
            receive(channel, selector, key, header, deadline);
            header.flip();
            int length = header.getInt();
            if (length <= 0 || length > MAX_FRAME) {
                throw new IOException("Invalid response frame length");
            }
            ByteBuffer body = ByteBuffer.allocate(length);
            receive(channel, selector, key, body, deadline);
            return body.array();
        }
    }

    private static void receive(SocketChannel channel, Selector selector, SelectionKey key,
            ByteBuffer destination, long deadline) throws IOException {
        while (destination.hasRemaining()) {
            checkDeadline(deadline);
            int count = channel.read(destination);
            if (count < 0) {
                throw new EOFException("Connection closed inside response frame");
            }
            if (count == 0) {
                await(selector, key, SelectionKey.OP_READ, deadline);
            }
        }
    }

    private static void await(Selector selector, SelectionKey key, int operation, long deadline)
            throws IOException {
        checkDeadline(deadline);
        key.interestOps(operation);
        selector.select(Math.max(1, TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime())));
        selector.selectedKeys().clear();
        checkDeadline(deadline);
    }

    private static void checkDeadline(long deadline) throws SocketTimeoutException {
        if (System.nanoTime() >= deadline) {
            throw new SocketTimeoutException("Framed transaction deadline exceeded");
        }
    }
}
