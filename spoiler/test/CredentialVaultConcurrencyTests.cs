using System;
using System.IO;
using System.Threading;

namespace Spoiler.Tests
{
    // This executable only uses the isolated directory supplied by the test.
    public static class CredentialVaultConcurrencyTests
    {
        private static LocoCredentials Bundle(string token)
        {
            return new LocoCredentials { OAuthToken = token, RefreshToken = "synthetic-refresh",
                UserId = "42", DeviceUuid = "0123456789abcdef0123456789abcdef", DeviceType = "tablet" };
        }

        public static int Main(string[] args)
        {
            try
            {
                if (args.Length < 2) throw new ArgumentException("An isolated directory and command are required.");
                CredentialVault vault = new CredentialVault(args[0], args.Length > 3 ? int.Parse(args[3]) : 10000);
                string command = args[1];
                if (command == "seed")
                {
                    vault.Save(Bundle("synthetic-csharp-token"));
                    Console.WriteLine(vault.LoadSnapshot().Revision);
                }
                else if (command == "read")
                {
                    CredentialSnapshot snapshot = vault.LoadSnapshot();
                    Console.WriteLine(snapshot.Revision);
                    Console.WriteLine(snapshot.Credentials == null ? "absent" : snapshot.Credentials.OAuthToken);
                }
                else if (command == "cas")
                {
                    CredentialSnapshot saved = vault.SaveIfUnchanged(Bundle("synthetic-csharp-rotated"), args[2]);
                    Console.WriteLine(saved.Revision);
                }
                else if (command == "hold")
                {
                    Directory.CreateDirectory(args[0]);
                    using (FileStream stream = new FileStream(Path.Combine(args[0], ".loco-credentials.dat.lock"),
                        FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite))
                    {
                        stream.Lock(0, 1);
                        Console.WriteLine("locked"); Console.Out.Flush();
                        Thread.Sleep(int.Parse(args[2]));
                        stream.Unlock(0, 1);
                    }
                }
                else if (command == "auth-hold")
                {
                    using (vault.AcquireAuthLease(10000, CancellationToken.None))
                    {
                        Console.WriteLine("locked"); Console.Out.Flush();
                        Thread.Sleep(int.Parse(args[2]));
                    }
                }
                else if (command == "auth-refresh")
                {
                    CredentialSnapshot baseline = vault.LoadSnapshot();
                    using (vault.AcquireAuthLease(10000, CancellationToken.None))
                    {
                        CredentialSnapshot latest = vault.LoadSnapshot();
                        if (latest.Credentials.UserId != baseline.Credentials.UserId || latest.Credentials.DeviceUuid != baseline.Credentials.DeviceUuid)
                            throw new InvalidOperationException("auth_identity_changed");
                        if (latest.Credentials.OAuthToken != baseline.Credentials.OAuthToken)
                            Console.WriteLine("reused");
                        else
                        {
                            Console.WriteLine("refreshing"); Console.Out.Flush();
                            File.AppendAllText(Path.Combine(args[0], "synthetic-remote.jsonl"), "csharp-refresh\n");
                            Thread.Sleep(int.Parse(args[2]));
                            vault.SaveIfUnchanged(Bundle("synthetic-csharp-winning"), latest.Revision);
                            Console.WriteLine("saved");
                        }
                    }
                }
                else if (command == "self-test")
                {
                    vault.Save(Bundle("synthetic-original"));
                    CredentialSnapshot first = vault.LoadSnapshot();
                    CredentialSnapshot second = new CredentialVault(args[0]).LoadSnapshot();
                    CredentialSnapshot winning = vault.SaveIfUnchanged(Bundle("synthetic-winning"), first.Revision);
                    bool conflicted = false;
                    try { vault.SaveIfUnchanged(Bundle("synthetic-stale"), second.Revision); }
                    catch (CredentialVaultConflictException) { conflicted = true; }
                    if (!conflicted || vault.Load().OAuthToken != "synthetic-winning") throw new Exception("Stale overwrite accepted.");
                    if (winning.Revision != vault.LoadSnapshot().Revision) throw new Exception("Saved revision differs.");
                    vault.Clear();
                    conflicted = false;
                    try { vault.SaveIfUnchanged(Bundle("synthetic-after-clear"), winning.Revision); }
                    catch (CredentialVaultConflictException) { conflicted = true; }
                    if (!conflicted || vault.Exists) throw new Exception("Deleted vault was resurrected.");
                    Console.WriteLine("Credential concurrency self-test passed.");
                }
                else throw new ArgumentException("Unknown test command.");
                return 0;
            }
            catch (CredentialVaultConflictException) { Console.WriteLine("credential_vault_conflict"); return 3; }
            catch (TimeoutException) { Console.WriteLine("credential_vault_lock_timeout"); return 4; }
            catch (Exception exception) { Console.WriteLine(exception.GetType().Name); return 1; }
        }
    }
}
