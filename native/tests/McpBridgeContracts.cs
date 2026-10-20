using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace SpatialPredictionStudio
{
    internal static class McpBridgeContracts
    {
        private static int count;
        private static void Check(bool good, string label) { if (!good) throw new Exception(label); count++; Console.WriteLine("PASS " + label); }
        private static void Reject(Action action, string label)
        {
            try { action(); } catch (InvalidDataException) { Check(true, label); return; }
            catch (UnauthorizedAccessException) { Check(true, label); return; }
            catch (ArgumentException) { Check(true, label); return; }
            throw new Exception("Expected rejection: " + label);
        }
        private static byte[] Bytes(object value) { return Encoding.UTF8.GetBytes(Json.Serializer().Serialize(value)); }
        private static Dictionary<string, object> Exchange(string pipeName, object value)
        {
            using (var client = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous))
            {
                client.Connect(5000);
                using (var timer = new CancellationTokenSource(5000))
                using (timer.Token.Register(() => client.Dispose()))
                {
                    var data = Bytes(value); client.Write(data, 0, data.Length); client.WriteByte(10); client.Flush();
                    using (var reader = new StreamReader(client, Encoding.UTF8))
                        return Json.Serializer().Deserialize<Dictionary<string, object>>(reader.ReadLine());
                }
            }
        }
        internal static int Run(string root)
        {
            count = 0;
            var request = new Dictionary<string, object> { { "id", "request1" }, { "token", "secret" }, { "method", "status" }, { "params", new Dictionary<string, object>() } };
            Check(Json.Text(McpPipeBridge.ParseRequest(Bytes(request), "secret"), "id") == "request1", "MCP authenticated closed request accepted");
            Reject(() => McpPipeBridge.ParseRequest(Bytes(request), "wrong"), "MCP wrong token rejected before dispatch");
            request["exec"] = "arbitrary"; Reject(() => McpPipeBridge.ParseRequest(Bytes(request), "secret"), "MCP unknown request key rejected"); request.Remove("exec");
            request["id"] = 12; Reject(() => McpPipeBridge.ParseRequest(Bytes(request), "secret"), "MCP non-string correlation ID rejected"); request["id"] = "request1";
            request["params"] = new object[0]; Reject(() => McpPipeBridge.ParseRequest(Bytes(request), "secret"), "MCP array parameters rejected"); request["params"] = new Dictionary<string, object>();
            Reject(() => McpPipeBridge.ParseRequest(new byte[McpPipeBridge.RequestLimit + 1], "secret"), "MCP oversized message rejected");
            Reject(() => McpPipeBridge.ParseRequest(new byte[] { 0xff }, "secret"), "MCP invalid UTF8 rejected");
            string local = Path.Combine(root, "mcp-local"), sessionFile;
            int dispatches = 0; var messages = new List<string>();
            using (var bridge = new McpPipeBridge(local, (method, parameters, token) => {
                Interlocked.Increment(ref dispatches);
                if (method == "reject") throw new InvalidDataException("UNKNOWN_COMMAND");
                return Task.FromResult<object>(new { method, thread = Thread.CurrentThread.ManagedThreadId });
            }, text => { lock (messages) messages.Add(text); }))
            {
                sessionFile = Path.Combine(local, "mcp", "sessions", bridge.SessionId + ".json");
                var session = Json.Read(sessionFile);
                Check(Json.Text(session, "pipe_name") == bridge.PipeName && Json.Text(session, "token").Length >= 40, "MCP private discovery stores random pipe and strong secret");
                var access = Directory.GetAccessControl(Path.GetDirectoryName(sessionFile));
                var owner = WindowsIdentity.GetCurrent().User;
                Check(access.AreAccessRulesProtected && access.GetAccessRules(true, true, typeof(SecurityIdentifier)).Cast<FileSystemAccessRule>().All(r => r.IdentityReference.Equals(owner)), "MCP discovery directory has only current user access");
                request["token"] = Json.Text(session, "token");
                var reply = Exchange(bridge.PipeName, request);
                Check((bool)reply["ok"] && Json.Text(reply, "id") == "request1" && dispatches == 1, "MCP actual named pipe returns correlated authenticated response");
                request["token"] = "bad"; reply = Exchange(bridge.PipeName, request);
                Check(!(bool)reply["ok"] && dispatches == 1 && Json.Text((Dictionary<string, object>)reply["error"], "code") == "ACCESS_DENIED", "MCP actual pipe rejects wrong secret without handler execution");
                request["token"] = Json.Text(session, "token"); request["method"] = "reject"; reply = Exchange(bridge.PipeName, request);
                Check(!(bool)reply["ok"] && Json.Text((Dictionary<string, object>)reply["error"], "code") == "COMMAND_FAILED", "MCP handler error becomes structured response");
                request["method"] = "status"; reply = Exchange(bridge.PipeName, request);
                Check((bool)reply["ok"] && dispatches == 3, "MCP connection survives rejected request");
                Check(!messages.Any(m => m.Contains(Json.Text(session, "token"))), "MCP transport log does not disclose authentication secret");
            }
            Check(!File.Exists(sessionFile), "MCP disposal removes owned discovery file");
            return count;
        }
    }
}
