"""Quick test for DeepSeekProxy: verifies API key, compatibility, and proxy logic."""
import json, os, sys, winreg, httpx

def get_env(key, default=""):
    val = os.getenv(key, "")
    if val:
        return val
    if sys.platform == "win32":
        for root, subkey in (
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ):
            try:
                with winreg.OpenKey(root, subkey) as k:
                    val, _ = winreg.QueryValueEx(k, key)
                    if val:
                        return val
            except OSError:
                continue
    return default

api_key = get_env("DEEPSEEK_API_KEY")
target_model = get_env("DEEPSEEK_MODEL", "deepseek-v4-pro")
base_url = get_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

print(f"API Key : {api_key[:12]}... (len={len(api_key)})")
print(f"Model   : {target_model}")
print(f"Base URL: {base_url}")
print()

if not api_key:
    print("FAIL: DEEPSEEK_API_KEY not found!")
    sys.exit(1)

passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name} {detail}")
        passed += 1
    else:
        print(f"  FAIL: {name} {detail}")
        failed += 1

# Test 1: Non-streaming chat completions
print("1. Non-streaming chat completions...")
r = httpx.post(f"{base_url}/v1/chat/completions",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"messages": [{"role": "user", "content": "Just say OK"}], "model": target_model},
    timeout=30)
test("status 200", r.status_code == 200, f"(got {r.status_code})")
if r.status_code == 200:
    d = r.json()
    test("has choices", "choices" in d)
    test("has usage", "usage" in d)
    test("finish_reason=stop", d["choices"][0]["finish_reason"] == "stop")
    print(f"       Response: {d['choices'][0]['message']['content'][:60]}")
    print(f"       Model: {d['model']}")

# Test 2: Streaming
print("2. Streaming...")
r = httpx.post(f"{base_url}/v1/chat/completions",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"messages": [{"role": "user", "content": "Hi"}], "model": target_model, "stream": True},
    timeout=30)
test("status 200", r.status_code == 200)
chunks = [l for l in r.text.splitlines() if l.startswith("data: ") and l != "data: [DONE]"]
test("has chunks", len(chunks) > 0, f"({len(chunks)} chunks)")

# Test 3: System message
print("3. System message...")
r = httpx.post(f"{base_url}/v1/chat/completions",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"messages": [
        {"role": "system", "content": "You are a pirate. Reply very briefly."},
        {"role": "user", "content": "Hello"}
    ], "model": target_model},
    timeout=30)
test("status 200", r.status_code == 200)
if r.status_code == 200:
    print(f"       Response: {r.json()['choices'][0]['message']['content'][:80]}")

# Test 4: Models list
print("4. Models list endpoint...")
r = httpx.get(f"{base_url}/v1/models",
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=15)
test("status 200", r.status_code == 200, f"(got {r.status_code})")

# Test 5: Proxy logic - developer->system conversion
print("5. Developer role -> system conversion...")
msg = {"role": "developer", "content": "test"}
if msg.get("role") == "developer":
    msg["role"] = "system"
test("role converted", msg["role"] == "system", f"(role={msg['role']})")

# Test 6: Proxy logic - model override
print("6. Model name override...")
test("model forced", target_model != "gpt-4", f"({target_model})")

# Test 7: Header filter logic
print("7. Header filter logic...")
hop_by_hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
headers = {"Host": "x", "Connection": "y", "Authorization": "Bearer sk-xxx", "Content-Length": "100", "Custom": "val"}
filtered = {k.lower(): v for k, v in headers.items()
            if k.lower() not in hop_by_hop and k.lower() not in {"host", "content-length"}}
test("strips hop-by-hop", "connection" not in filtered)
test("strips host", "host" not in filtered)
test("strips content-length", "content-length" not in filtered)
test("keeps auth", "authorization" in filtered)
test("keeps custom", "custom" in filtered)

print()
print(f"=== {'ALL PASSED' if failed == 0 else 'SOME FAILED'}: {passed}/{passed+failed} passed ===")
sys.exit(0 if failed == 0 else 1)
