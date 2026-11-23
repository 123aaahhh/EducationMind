import httpx, json, sys

msg = sys.argv[1] if len(sys.argv) > 1 else "VPN 连不上，账号也被锁了"
r = httpx.post(
    "http://127.0.0.1:8000/chat",
    json={"message": msg, "user_id": "t", "conv_id": "cx"},
    timeout=120,
)
d = r.json()
out = {
    "http": r.status_code,
    "intent": d.get("intent"),
    "primary": d.get("primary_agent"),
    "supporting": d.get("supporting_agents"),
    "reason": d.get("routing_reason"),
}
print(json.dumps(out, ensure_ascii=False, indent=2))
