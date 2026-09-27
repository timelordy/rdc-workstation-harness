using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Events;
using Microsoft.CSharp;
using System.CodeDom.Compiler;
using System.Web.Script.Serialization;

namespace ChatGPT.RevitBridge
{
    public class App : IExternalApplication
    {
        private static readonly int Port = ReadPort();
        private static readonly ConcurrentQueue<WorkItem> Queue = new ConcurrentQueue<WorkItem>();
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024 };
        private static readonly Dictionary<string, MethodInfo> ScriptCache = new Dictionary<string, MethodInfo>();
        private static readonly object CacheLock = new object();

        private TcpListener _listener;
        private Thread _serverThread;
        private volatile bool _running;
        private string _token;
        private DateTime _startedUtc;

        public Result OnStartup(UIControlledApplication application)
        {
            _startedUtc = DateTime.UtcNow;
            _token = LoadOrCreateToken();
            application.Idling += OnIdling;
            _listener = new TcpListener(IPAddress.Loopback, Port);
            _listener.Start();
            _running = true;
            _serverThread = new Thread(ServerLoop) { IsBackground = true, Name = "ChatGPT.RevitBridge" };
            _serverThread.Start();
            return Result.Succeeded;
        }

        public Result OnShutdown(UIControlledApplication application)
        {
            application.Idling -= OnIdling;
            _running = false;
            try { if (_listener != null) _listener.Stop(); } catch { }
            return Result.Succeeded;
        }
        private static int ReadPort()
        {
            int port;
            var value = Environment.GetEnvironmentVariable("REVIT_BRIDGE_PORT");
            return Int32.TryParse(value, out port) ? port : 17323;
        }

        private static string TokenPath()
        {
            var dir = Environment.GetEnvironmentVariable("RDC_HARNESS_TOKEN_DIR");
            if (String.IsNullOrWhiteSpace(dir))
            {
                dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    ".chatgpt-desktop-agent");
            }
            Directory.CreateDirectory(dir);
            return Path.Combine(dir, "revit.token");
        }

        private static string LoadOrCreateToken()
        {
            var path = TokenPath();
            if (File.Exists(path))
            {
                var existing = File.ReadAllText(path, Encoding.UTF8).Trim();
                if (existing.Length >= 32) return existing;
            }

            var token = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
            File.WriteAllText(path, token, Encoding.UTF8);
            return token;
        }

        private void ServerLoop()
        {
            while (_running)
            {
                try
                {
                    using (var client = _listener.AcceptTcpClient())
                    {
                        client.ReceiveTimeout = 130000;
                        client.SendTimeout = 130000;
                        HandleClient(client);
                    }
                }
                catch (SocketException)
                {
                    if (!_running) break;
                }
                catch { }
            }
        }

        private void HandleClient(TcpClient client)
        {
            var stream = client.GetStream();
            var request = ReadHttpRequest(stream);
            if (request == null)
            {
                WriteHttp(stream, 400, Error("bad request"));
                return;
            }

            if (request.Path == "/health" && request.Method == "GET")
            {
                WriteHttp(stream, 200, Json.Serialize(new Dictionary<string, object> {
                    { "ok", true },
                    { "bridge", "revit" },
                    { "pid", System.Diagnostics.Process.GetCurrentProcess().Id },
                    { "port", Port },
                    { "uptime_sec", (DateTime.UtcNow - _startedUtc).TotalSeconds }
                }));
                return;
            }

            if (request.Method != "POST" || request.Path != "/action")
            {
                WriteHttp(stream, 404, Error("not found"));
                return;
            }

            string supplied;
            request.Headers.TryGetValue("x-desktop-agent-token", out supplied);
            if (!String.Equals(supplied, _token, StringComparison.Ordinal))
            {
                WriteHttp(stream, 403, Error("invalid token"));
                return;
            }
            Dictionary<string, object> payload;
            try
            {
                payload = Json.Deserialize<Dictionary<string, object>>(request.Body ?? "{}");
            }
            catch (Exception ex)
            {
                WriteHttp(stream, 400, Error("invalid json: " + ex.Message));
                return;
            }

            var item = new WorkItem { Request = payload };
            Queue.Enqueue(item);

            if (!item.Done.Wait(TimeSpan.FromSeconds(120)))
            {
                WriteHttp(stream, 504, Error("Revit main-thread execution timed out"));
                return;
            }

            WriteHttp(stream, item.StatusCode, item.Response ?? Error("empty response"));
        }

        private void OnIdling(object sender, IdlingEventArgs e)
        {
            var uiapp = sender as UIApplication;
            if (uiapp == null) return;

            int processed = 0;
            WorkItem item;
            while (processed < 4 && Queue.TryDequeue(out item))
            {
                try
                {
                    item.Response = Execute(uiapp, item.Request);
                    item.StatusCode = 200;
                }
                catch (Exception ex)
                {
                    item.StatusCode = 500;
                    item.Response = Error(ex.GetType().Name + ": " + ex.Message);
                }
                finally
                {
                    item.Done.Set();
                }
                processed++;
            }

            if (!Queue.IsEmpty)
            {
                try { e.SetRaiseWithoutDelay(); } catch { }
            }
        }

        private string Execute(UIApplication uiapp, Dictionary<string, object> request)
        {
            var action = GetString(request, "action");
            if (action == "status")
                return Status(uiapp);

            if (action == "csharp")
            {
                var code = GetString(request, "code");
                var args = GetDictionary(request, "args");
                var result = RunScript(uiapp, code, args);
                return SerializeOk(result);
            }

            throw new InvalidOperationException("unknown action: " + action);
        }
        private string Status(UIApplication uiapp)
        {
            var uidoc = uiapp.ActiveUIDocument;
            var doc = uidoc != null ? uidoc.Document : null;
            var docs = new List<object>();
            foreach (Document d in uiapp.Application.Documents)
            {
                docs.Add(new Dictionary<string, object> {
                    { "title", d.Title },
                    { "path", SafePath(d) },
                    { "is_family", d.IsFamilyDocument },
                    { "is_modified", d.IsModified }
                });
            }

            return Json.Serialize(new Dictionary<string, object> {
                { "ok", true },
                { "result", new Dictionary<string, object> {
                    { "version_name", uiapp.Application.VersionName },
                    { "version_number", uiapp.Application.VersionNumber },
                    { "active_document", doc == null ? null : new Dictionary<string, object> {
                        { "title", doc.Title },
                        { "path", SafePath(doc) },
                        { "is_modified", doc.IsModified }
                    }},
                    { "documents", docs }
                }}
            });
        }

        private static string SafePath(Document d)
        {
            try { return d.PathName; } catch { return null; }
        }

        private object RunScript(UIApplication uiapp, string body, Dictionary<string, object> args)
        {
            if (String.IsNullOrWhiteSpace(body))
                throw new ArgumentException("code is required");

            MethodInfo method;
            lock (CacheLock)
            {
                if (!ScriptCache.TryGetValue(body, out method))
                {
                    method = Compile(body);
                    ScriptCache[body] = method;
                }
            }

            try
            {
                return method.Invoke(null, new object[] { uiapp, args ?? new Dictionary<string, object>() });
            }
            catch (TargetInvocationException ex)
            {
                throw ex.InnerException ?? ex;
            }
        }
        private MethodInfo Compile(string body)
        {
            var source = @"
using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

public static class DynamicRunner
{
    public static object Run(UIApplication uiapp, IDictionary<string, object> args)
    {
        var uidoc = uiapp.ActiveUIDocument;
        var doc = uidoc != null ? uidoc.Document : null;
" + body + @"
    }
}";

            using (var provider = new CSharpCodeProvider())
            {
                var cp = new CompilerParameters {
                    GenerateExecutable = false,
                    GenerateInMemory = true,
                    TreatWarningsAsErrors = false
                };
                cp.ReferencedAssemblies.Add("System.dll");
                cp.ReferencedAssemblies.Add("System.Core.dll");
                cp.ReferencedAssemblies.Add("Microsoft.CSharp.dll");
                cp.ReferencedAssemblies.Add(typeof(UIApplication).Assembly.Location);
                cp.ReferencedAssemblies.Add(typeof(Element).Assembly.Location);

                var result = provider.CompileAssemblyFromSource(cp, source);
                if (result.Errors.HasErrors)
                {
                    var sb = new StringBuilder();
                    foreach (CompilerError error in result.Errors)
                        sb.AppendLine(error.ToString());
                    throw new InvalidOperationException("C# compile failed:\n" + sb);
                }

                var type = result.CompiledAssembly.GetType("DynamicRunner", true);
                return type.GetMethod("Run", BindingFlags.Public | BindingFlags.Static);
            }
        }

        private string SerializeOk(object value)
        {
            try
            {
                return Json.Serialize(new Dictionary<string, object> {
                    { "ok", true },
                    { "result", value }
                });
            }
            catch
            {
                return Json.Serialize(new Dictionary<string, object> {
                    { "ok", true },
                    { "result", value == null ? null : value.ToString() },
                    { "result_type", value == null ? null : value.GetType().FullName }
                });
            }
        }
        private static string GetString(Dictionary<string, object> d, string key)
        {
            object value;
            if (!d.TryGetValue(key, out value) || value == null) return null;
            return Convert.ToString(value);
        }

        private static Dictionary<string, object> GetDictionary(Dictionary<string, object> d, string key)
        {
            object value;
            if (!d.TryGetValue(key, out value) || value == null)
                return new Dictionary<string, object>();
            var dict = value as Dictionary<string, object>;
            if (dict != null) return dict;
            var generic = value as IDictionary<string, object>;
            if (generic != null) return new Dictionary<string, object>(generic);
            return new Dictionary<string, object>();
        }

        private static string Error(string message)
        {
            return Json.Serialize(new Dictionary<string, object> {
                { "ok", false },
                { "error", message }
            });
        }

        private static HttpRequest ReadHttpRequest(NetworkStream stream)
        {
            var headerBytes = new List<byte>();
            int state = 0;
            while (headerBytes.Count < 65536)
            {
                int b = stream.ReadByte();
                if (b < 0) return null;
                headerBytes.Add((byte)b);
                if (state == 0 && b == 13) state = 1;
                else if (state == 1 && b == 10) state = 2;
                else if (state == 2 && b == 13) state = 3;
                else if (state == 3 && b == 10) break;
                else state = 0;
            }

            var headerText = Encoding.ASCII.GetString(headerBytes.ToArray());
            var lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
            if (lines.Length == 0) return null;
            var first = lines[0].Split(' ');
            if (first.Length < 2) return null;

            var req = new HttpRequest {
                Method = first[0].ToUpperInvariant(),
                Path = first[1],
                Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            };

            for (int i = 1; i < lines.Length; i++)
            {
                var line = lines[i];
                var idx = line.IndexOf(':');
                if (idx <= 0) continue;
                req.Headers[line.Substring(0, idx).Trim().ToLowerInvariant()] = line.Substring(idx + 1).Trim();
            }

            int length = 0;
            string lenText;
            if (req.Headers.TryGetValue("content-length", out lenText))
                Int32.TryParse(lenText, out length);
            if (length > 8 * 1024 * 1024) throw new InvalidOperationException("payload too large");

            if (length > 0)
            {
                var body = new byte[length];
                int offset = 0;
                while (offset < length)
                {
                    int n = stream.Read(body, offset, length - offset);
                    if (n <= 0) break;
                    offset += n;
                }
                req.Body = Encoding.UTF8.GetString(body, 0, offset);
            }
            return req;
        }
        private static void WriteHttp(NetworkStream stream, int status, string body)
        {
            var payload = Encoding.UTF8.GetBytes(body ?? "");
            var reason = status == 200 ? "OK" :
                         status == 400 ? "Bad Request" :
                         status == 403 ? "Forbidden" :
                         status == 404 ? "Not Found" :
                         status == 504 ? "Gateway Timeout" :
                         "Internal Server Error";
            var headers =
                "HTTP/1.1 " + status + " " + reason + "\r\n" +
                "Content-Type: application/json; charset=utf-8\r\n" +
                "Content-Length: " + payload.Length + "\r\n" +
                "Connection: close\r\n\r\n";
            var head = Encoding.ASCII.GetBytes(headers);
            stream.Write(head, 0, head.Length);
            stream.Write(payload, 0, payload.Length);
            stream.Flush();
        }

        private sealed class WorkItem
        {
            public Dictionary<string, object> Request;
            public readonly ManualResetEventSlim Done = new ManualResetEventSlim(false);
            public int StatusCode = 500;
            public string Response;
        }

        private sealed class HttpRequest
        {
            public string Method;
            public string Path;
            public Dictionary<string, string> Headers;
            public string Body;
        }
    }
}
