using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace SpatialPredictionStudio
{
    // Transport only. All ArcObjects calls are dispatched by StudioControl on its owner thread.
    internal sealed class McpPipeBridge : IDisposable
    {
        internal const int RequestLimit = 1024 * 1024, ResponseLimit = 8 * 1024 * 1024;
        private readonly CancellationTokenSource stopped = new CancellationTokenSource();
        private readonly Func<string, Dictionary<string, object>, CancellationToken, Task<object>> dispatch;
        private readonly Action<string> log;
        private readonly string token, sessionPath;
        private readonly SecurityIdentifier owner;
        private NamedPipeServerStream active;
        private readonly object gate = new object();
        internal readonly string SessionId, PipeName;

        internal McpPipeBridge(string localRoot, Func<string, Dictionary<string, object>, CancellationToken, Task<object>> handler, Action<string> logger)
        {
            dispatch = handler; log = logger; owner = WindowsIdentity.GetCurrent().User;
            SessionId = Guid.NewGuid().ToString("N"); PipeName = "SPS-" + Process.GetCurrentProcess().Id + "-" + SessionId;
            var secret = new byte[32]; using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(secret);
            token = Convert.ToBase64String(secret);
            string directory = Path.Combine(localRoot, "mcp", "sessions");
            Directory.CreateDirectory(directory);
            var access = new DirectorySecurity(); access.SetAccessRuleProtection(true, false); access.SetOwner(owner);
            access.AddAccessRule(new FileSystemAccessRule(owner, FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            new DirectoryInfo(directory).SetAccessControl(access);
            sessionPath = Path.Combine(directory, SessionId + ".json");
            Json.Write(sessionPath, new Dictionary<string, object> {
                { "schema_version", 1 }, { "session_id", SessionId }, { "pid", Process.GetCurrentProcess().Id },
                { "pipe_name", PipeName }, { "token", token }, { "created_utc", DateTime.UtcNow.ToString("o") },
                { "process_start_utc", Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("o") },
                { "addin_version", typeof(McpPipeBridge).Assembly.GetName().Version.ToString() }
            });
            Task.Run((Func<Task>)Serve);
        }
        private static bool EqualToken(string a, string b)
        {
            if (a == null || b == null || a.Length != b.Length) return false;
            int difference = 0; for (int i = 0; i < a.Length; i++) difference |= a[i] ^ b[i]; return difference == 0;
        }
        internal static Dictionary<string, object> ParseRequest(byte[] bytes, string expectedToken)
        {
            if (bytes.Length == 0 || bytes.Length > RequestLimit) throw new InvalidDataException("REQUEST_SIZE");
            var request = Json.Serializer().Deserialize<Dictionary<string, object>>(new UTF8Encoding(false, true).GetString(bytes));
            if (request == null || request.Keys.Except(new[] { "id", "token", "method", "params" }).Any()) throw new InvalidDataException("REQUEST_SCHEMA");
            if (!EqualToken(Json.Text(request, "token"), expectedToken)) throw new UnauthorizedAccessException("AUTHENTICATION_FAILED");
            if (!(request.ContainsKey("id") && request["id"] is string) || Json.Text(request, "id").Length > 128 || Json.Text(request, "id").Length == 0) throw new InvalidDataException("REQUEST_ID");
            if (!(request.ContainsKey("params") && request["params"] is Dictionary<string, object>)) throw new InvalidDataException("REQUEST_PARAMS");
            if (!(request.ContainsKey("method") && request["method"] is string) || Json.Text(request, "method").Length > 80) throw new InvalidDataException("REQUEST_METHOD");
            return request;
        }
        private async Task Serve()
        {
            while (!stopped.IsCancellationRequested)
            {
                try
                {
                    var security = new PipeSecurity(); security.SetAccessRuleProtection(true, false); security.SetOwner(owner);
                    security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.NetworkSid, null), PipeAccessRights.FullControl, AccessControlType.Deny));
                    security.AddAccessRule(new PipeAccessRule(owner, PipeAccessRights.FullControl, AccessControlType.Allow));
                    using (var pipe = new NamedPipeServerStream(PipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 65536, 65536, security))
                    {
                        lock (gate) { if (stopped.IsCancellationRequested) return; active = pipe; }
                        await pipe.WaitForConnectionAsync(stopped.Token).ConfigureAwait(false);
                        using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(stopped.Token))
                        {
                            deadline.CancelAfter(TimeSpan.FromSeconds(30));
                            using (deadline.Token.Register(() => { try { pipe.Dispose(); } catch (ObjectDisposedException) { } }))
                            {
                                string id = null; object response;
                                try
                                {
                                    var request = ParseRequest(await ReadLine(pipe, deadline.Token).ConfigureAwait(false), token);
                                    id = Json.Text(request, "id"); string method = Json.Text(request, "method");
                                    var result = await dispatch(method, (Dictionary<string, object>)request["params"], deadline.Token).ConfigureAwait(false);
                                    response = new Dictionary<string, object> { { "id", id }, { "ok", true }, { "result", result } };
                                }
                                catch (Exception ex)
                                {
                                    string code = ex is UnauthorizedAccessException ? "ACCESS_DENIED" : ex is OperationCanceledException ? "REQUEST_TIMEOUT" : "COMMAND_FAILED";
                                    response = new Dictionary<string, object> { { "id", id }, { "ok", false }, { "error", new Dictionary<string, object> { { "code", code }, { "message", ex.Message } } } };
                                }
                                byte[] reply = Encoding.UTF8.GetBytes(Json.Serializer().Serialize(response) + "\n");
                                if (reply.Length > ResponseLimit) reply = Encoding.UTF8.GetBytes(Json.Serializer().Serialize(new { id, ok = false, error = new { code = "RESPONSE_TOO_LARGE", message = "Daha küçük bir sonuç sayfası isteyin." } }) + "\n");
                                await pipe.WriteAsync(reply, 0, reply.Length, deadline.Token).ConfigureAwait(false);
                                await pipe.FlushAsync(deadline.Token).ConfigureAwait(false);
                            }
                        }
                    }
                }
                catch (Exception ex) { if (!stopped.IsCancellationRequested) { log("MCP bağlantısı kapandı: " + ex.GetType().Name); await Task.Delay(100).ConfigureAwait(false); } }
                finally { lock (gate) active = null; }
            }
        }
        private static async Task<byte[]> ReadLine(Stream stream, CancellationToken token)
        {
            using (var data = new MemoryStream())
            {
                byte[] chunk = new byte[4096];
                while (true)
                {
                    int count = await stream.ReadAsync(chunk, 0, chunk.Length, token).ConfigureAwait(false);
                    if (count == 0) throw new EndOfStreamException("REQUEST_TRUNCATED");
                    int newline = Array.IndexOf(chunk, (byte)10, 0, count), used = newline < 0 ? count : newline;
                    if (data.Length + used > RequestLimit) throw new InvalidDataException("REQUEST_SIZE");
                    data.Write(chunk, 0, used);
                    if (newline >= 0) { if (newline != count - 1) throw new InvalidDataException("ONE_REQUEST_PER_CONNECTION"); return data.ToArray(); }
                }
            }
        }
        public void Dispose()
        {
            if (stopped.IsCancellationRequested) return;
            stopped.Cancel(); lock (gate) { try { active?.Dispose(); } catch (ObjectDisposedException) { } }
            // Only this instance's random, protected session file is removed.
            try { if (File.Exists(sessionPath) && EqualToken(Json.Text(Json.Read(sessionPath), "token"), token)) File.Delete(sessionPath); }
            catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
    }
}
