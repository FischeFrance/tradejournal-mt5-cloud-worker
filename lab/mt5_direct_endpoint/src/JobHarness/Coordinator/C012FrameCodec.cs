using System.Buffers.Binary;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// Thrown for a frame that violates the wire framing contract: a declared length outside
// the allowed range, or a stream that ends mid-header or mid-payload. A clean end-of-stream
// observed before any header byte arrives is not an error: ReadFrameAsync returns null for
// that case instead of throwing.
public class C012FramingException : Exception
{
    public C012FramingException()
    {
    }

    public C012FramingException(string message)
        : base(message)
    {
    }

    public C012FramingException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}

// Length-prefixed message framing over any Stream: a real named pipe in production, a
// MemoryStream in tests. Stream.ReadAsync is not guaranteed to fill its buffer in a single
// call, so both the 4-byte length prefix and the payload are read in a loop.
public static class C012FrameCodec
{
    public const int MaxFrameLength = 8192;
    private const int LengthPrefixSize = 4;

    public static async ValueTask WriteFrameAsync(
        Stream stream, ReadOnlyMemory<byte> payload, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(stream);
        if (payload.Length is < 1 or > MaxFrameLength)
        {
            throw new ArgumentOutOfRangeException(
                nameof(payload), "Frame payload length is out of the allowed range.");
        }

        byte[] header = new byte[LengthPrefixSize];
        BinaryPrimitives.WriteUInt32BigEndian(header, (uint)payload.Length);
        await stream.WriteAsync(header, cancellationToken).ConfigureAwait(false);
        await stream.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    // Returns null only for a clean end-of-stream observed before any header byte arrives.
    // Any other short read (mid-header or mid-payload) or an out-of-range declared length
    // throws C012FramingException. The length check happens immediately after the header is
    // read and before the payload buffer is allocated.
    public static async ValueTask<byte[]?> ReadFrameAsync(Stream stream, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(stream);

        byte[] header = new byte[LengthPrefixSize];
        int headerRead = await ReadExactAsync(stream, header, cancellationToken).ConfigureAwait(false);
        if (headerRead == 0)
        {
            return null;
        }

        if (headerRead < LengthPrefixSize)
        {
            throw new C012FramingException("Stream ended mid-length-prefix.");
        }

        uint declaredLength = BinaryPrimitives.ReadUInt32BigEndian(header);
        if (declaredLength == 0 || declaredLength > MaxFrameLength)
        {
            throw new C012FramingException("Declared frame length is out of the allowed range.");
        }

        byte[] payload = new byte[declaredLength];
        int payloadRead = await ReadExactAsync(stream, payload, cancellationToken).ConfigureAwait(false);
        if (payloadRead < payload.Length)
        {
            throw new C012FramingException("Stream ended mid-payload.");
        }

        return payload;
    }

    // Loops until the buffer is full or the stream ends; returns the number of bytes
    // actually read, which is less than buffer.Length only at a genuine end-of-stream.
    private static async ValueTask<int> ReadExactAsync(Stream stream, byte[] buffer, CancellationToken cancellationToken)
    {
        int totalRead = 0;
        while (totalRead < buffer.Length)
        {
            int read = await stream.ReadAsync(buffer.AsMemory(totalRead), cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                break;
            }

            totalRead += read;
        }

        return totalRead;
    }
}
