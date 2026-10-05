"""端到端自测：封签检查省略 seal_intact 的默认值修复。

覆盖：
  * 新提交：省略 seal_intact（或缺省字段、传 null）按完好（true）落库并参与核验，
    封签号相符的完整链可核定入库；明确提交 false 仍判封签破损、结论无效、核定拒绝；
  * 单事件上报与替换纠错的核验结论、事件内容（payload）与新版本快照三者一致；
  * 幂等/冲突：新提交同号省略重试 200 返回原结果，显式 true/false 之间仍 409，
    异内容仍 409；失败提交不产生新事件、新版本或纠错记录；
  * 历史兼容（以修复前归一化规则在进程内模拟旧库已落库行）：已落库事件、纠错记录、
    历史版本快照与封存案卷保持原样（仍为 false / 旧结论），不重算历史指纹；
    修复前同号同请求重试仍返回原结果，显式 true/异内容仍报冲突；
  * 启动可用：init_db 幂等、HTTP API 保持可调用。

运行：python -m tests.test_seal_default
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_seal_default.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

import app.service as svc  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()  # 触发 lifespan（启动钩子）
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reg(code, collector="张三", seal="SEAL-X"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": "2026-09-30T01:00:00Z",
        "collector": collector, "seal_no": seal})


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def chain(code):
    return client.get(f"{API}/samples/{code}/chain").json()


def versions(code):
    return client.get(f"{API}/samples/{code}/versions").json()


def snapshot(code, version=None):
    v = "latest" if version is None else str(version)
    return client.get(f"{API}/samples/{code}/versions/{v}").json()["version_snapshot"]


def correction(body):
    return client.post(f"{API}/corrections", json=body)


def approve(code):
    return client.post(f"{API}/samples/{code}/approve", json={"approver": "质量主管"})


def seal_payload(code, event_id):
    for e in chain(code)["timeline"]:
        if e["event_id"] == event_id:
            return e["payload"]
    raise KeyError(event_id)


def full_chain_before_check(code, seal="SEAL-X", collector="张三", receiver="李四"):
    """移交→签收（不含封签检查/入库），供封签检查用例在此之上补齐。"""
    reg(code, collector=collector, seal=seal)
    ev({"event_id": f"{code}-T", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": f"{code}-TR",
        "from_custodian": collector, "to_custodian": receiver})
    ev({"event_id": f"{code}-R", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": f"{code}-TR",
        "receiver": receiver, "seal_no": seal})


def storage(code, custodian="实验室", when="2026-09-30T04:00:00Z"):
    return ev({"event_id": f"{code}-S", "event_type": "storage", "sample_code": code,
               "occurred_at": when, "custodian": custodian})


def check_event(code="SD-NEW", seal="SEAL-X"):
    return {"event_id": f"{code}-C", "event_type": "seal_check", "sample_code": code,
            "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": seal}


# ---------------------------------------------------------------- 1. 省略按完好处理
print("1) 新提交省略 seal_intact：按完好落库、链可核定")
full_chain_before_check("SD-NEW")
r = ev(check_event())
check("省略字段返回 201", r.status_code == 201, r.text)
check("省略字段落库 seal_intact=true", seal_payload("SD-NEW", "SD-NEW-C")["seal_intact"] is True,
      str(seal_payload("SD-NEW", "SD-NEW-C")))
storage("SD-NEW")
check("省略字段不产生破损原因，结论 valid",
      chain("SD-NEW")["status"] == "valid"
      and all("破损" not in x for x in chain("SD-NEW")["reasons"]), str(chain("SD-NEW")["reasons"]))
r = approve("SD-NEW")
check("完整链核定入库 200", r.status_code == 200, r.text)

# 显式传 null 与省略等价
full_chain_before_check("SD-NULL")
r = ev({**check_event("SD-NULL"), "seal_intact": None})
check("显式 null 返回 201 且落库 true",
      r.status_code == 201 and seal_payload("SD-NULL", "SD-NULL-C")["seal_intact"] is True, r.text)

# 显式 true 与省略指纹一致：同号互换仍为幂等
full_chain_before_check("SD-TRUE")
r = ev({**check_event("SD-TRUE"), "seal_intact": True})
check("显式 true 落库 true", r.status_code == 201
      and seal_payload("SD-TRUE", "SD-TRUE-C")["seal_intact"] is True, r.text)
r = ev(check_event("SD-TRUE"))
check("省略重试显式 true 同号 → 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)

# ---------------------------------------------------------------- 2. 明确 false 仍判破损
print("2) 明确提交 seal_intact=false：封签破损 → 无效、核定拒绝")
full_chain_before_check("SD-BROKEN")
ev({**check_event("SD-BROKEN"), "seal_intact": False})
storage("SD-BROKEN")
c = chain("SD-BROKEN")
check("破损仍判 invalid 且原因含破损",
      c["status"] == "invalid" and any("封签破损/不完好" in x for x in c["reasons"]), str(c["reasons"]))
check("无效链核定 422", approve("SD-BROKEN").status_code == 422)
check("破损行 payload 保持 false", seal_payload("SD-BROKEN", "SD-BROKEN-C")["seal_intact"] is False)

# 同号：显式 true vs 已落库 false → 冲突（两种都是明确提交，新旧行为一致）
r = ev({**check_event("SD-BROKEN"), "seal_intact": True})
check("显式 true 对 false 行 → 409 event_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "event_conflict", r.text)
# 同号：封签号改动 → 异内容冲突
r = ev({**check_event("SD-BROKEN"), "checked_seal_no": "SEAL-OTHER"})
check("异内容（封签号不同）→ 409", r.status_code == 409, r.text)
# 同号：省略重试 false 行 → 幂等（修复前 bool(None)=False 与显式 false 同形态）
r = ev(check_event("SD-BROKEN"))
check("省略重试 false 行 → 200 幂等返回原行",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["event"]["payload"]["seal_intact"] is False, r.text)

# ---------------------------------------------------------------- 3. 失败提交不留事件/版本
print("3) 冲突失败不留下新事件或新版本")
v_before = versions("SD-BROKEN")["current_version"]
r = ev({**check_event("SD-BROKEN"), "occurred_at": "2026-09-30T03:31:00Z"})
check("异内容 409", r.status_code == 409, r.text)
check("失败不产生新版本", versions("SD-BROKEN")["current_version"] == v_before)
check("失败不新增事件（时间线数量不变）",
      len(chain("SD-BROKEN")["timeline"]) == 4)  # 移交/签收/检查/入库

# ---------------------------------------------------------------- 4. 新版本快照与链一致
print("4) 事件内容、核验结论与新版本快照一致")
full_chain_before_check("SD-SNAP")
v0 = versions("SD-SNAP")["current_version"]
r = ev(check_event("SD-SNAP"))
check("封签检查真正提交版本 +1", r.json()["evaluation"]["current_version"] == v0 + 1, r.text)
snap = snapshot("SD-SNAP")
snap_check = next(e for e in snap["timeline"] if e["event_id"] == "SD-SNAP-C")
check("快照内检查事件 payload=true", snap_check["payload"]["seal_intact"] is True,
      str(snap_check["payload"]))
storage("SD-SNAP")
snap = snapshot("SD-SNAP")
check("入库后最新快照 valid 且时间线 payload=true",
      snap["status"] == "valid"
      and next(e for e in snap["timeline"] if e["event_id"] == "SD-SNAP-C")["payload"]["seal_intact"] is True,
      str(snap["status"]))
# 幂等重试不产生新版本
v_now = versions("SD-SNAP")["current_version"]
ev(check_event("SD-SNAP"))
check("同号重试版本不增", versions("SD-SNAP")["current_version"] == v_now)
# 旧快照不可变：检查提交时点的上一版本（缺入库）仍为 pending
old = snapshot("SD-SNAP", v_now - 1)
check("历史快照不可变（入库前版本仍 pending）",
      old["status"] == "pending"
      and next(e for e in old["timeline"] if e["event_id"] == "SD-SNAP-C")["payload"]["seal_intact"] is True,
      str((old["status"],)))

# ---------------------------------------------------------------- 5. 替换纠错：省略按完好
print("5) 替换纠错的新事件省略 seal_intact：核验/内容/快照一致")
full_chain_before_check("SD-FIX")
# 原检查读错封签号 → invalid
ev({"event_id": "SD-FIX-C", "event_type": "seal_check", "sample_code": "SD-FIX",
    "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-WRONG", "seal_intact": True})
check("替换前 invalid", chain("SD-FIX")["status"] == "invalid")
v_before_fix = versions("SD-FIX")["current_version"]
fix_body = {
    "correction_id": "SD-FIX-1", "sample_code": "SD-FIX", "target_event_id": "SD-FIX-C",
    "action": "replace", "operator": "纠错员", "reason": "扫码识别错误，整体替换",
    "new_event": {"event_id": "SD-FIX-C2", "event_type": "seal_check",
                  "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-X"}}
r = correction(fix_body)
check("替换返回 201", r.status_code == 201, r.text)
new_ev = next(e for e in r.json()["evaluation"]["timeline"] if e["event_id"] == "SD-FIX-C2")
check("纠错响应中新事件内容 seal_intact=true", new_ev["payload"]["seal_intact"] is True,
      str(new_ev["payload"]))
check("替换使版本 +1", r.json()["evaluation"]["current_version"] == v_before_fix + 1, r.text)
storage("SD-FIX")
c = chain("SD-FIX")
check("替换补齐入库后 valid", c["status"] == "valid", str(c["reasons"]))
check("有效链含新号、旧号在 superseded",
      any(e["event_id"] == "SD-FIX-C2" for e in c["timeline"])
      and any(e["event_id"] == "SD-FIX-C" for e in c["superseded_events"]))
snap = snapshot("SD-FIX")
snap_new = next(e for e in snap["timeline"] if e["event_id"] == "SD-FIX-C2")
check("新版本快照与核验/内容一致（true、valid）",
      snap["status"] == "valid" and snap_new["payload"]["seal_intact"] is True
      and snap_new["replaces_event_id"] == "SD-FIX-C", str(snap_new))
check("替换后可核定入库", approve("SD-FIX").status_code == 200)

# 同纠错号同内容（省略）重试 → 200 幂等
v_before_retry = versions("SD-FIX")["current_version"]
r = correction(fix_body)
check("同纠错号省略重试 200 幂等", r.status_code == 200, r.text)
check("幂等重试不产生纠错新版本", versions("SD-FIX")["current_version"] == v_before_retry)

# 异内容仍冲突，且不留新纠错记录/版本
corr_total = client.get(f"{API}/samples/SD-FIX/corrections").json()["total"]
diff_body = {**fix_body, "reason": "原因被篡改"}
r = correction(diff_body)
check("同纠错号异内容 → 409 correction_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "correction_conflict", r.text)
check("冲突不新增纠错记录", client.get(f"{API}/samples/SD-FIX/corrections").json()["total"] == corr_total)
# 显式 true 与省略新指纹一致 → 仍幂等
true_body = {**fix_body,
             "new_event": {**fix_body["new_event"], "seal_intact": True}}
check("显式 true 重试省略纠错单 → 200 幂等", correction(true_body).status_code == 200)

# 替换纠错显式 false：新事件仍判破损
full_chain_before_check("SD-FIXF")
ev({"event_id": "SD-FIXF-C", "event_type": "seal_check", "sample_code": "SD-FIXF",
    "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-WRONG", "seal_intact": True})
r = correction({"correction_id": "SD-FIXF-1", "sample_code": "SD-FIXF", "target_event_id": "SD-FIXF-C",
                "action": "replace", "operator": "纠错员", "reason": "重报检查",
                "new_event": {"event_id": "SD-FIXF-C2", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:30:00Z",
                              "checked_seal_no": "SEAL-X", "seal_intact": False}})
check("替换为显式破损返回 201", r.status_code == 201, r.text)
storage("SD-FIXF")
check("显式 false 的替换链仍 invalid", chain("SD-FIXF")["status"] == "invalid"
      and any("破损" in x for x in chain("SD-FIXF")["reasons"]), str(chain("SD-FIXF")["reasons"]))
check("显式 false 核定拒绝", approve("SD-FIXF").status_code == 422)

# ---------------------------------------------------------------- 6. 历史数据：修复前旧行保持原样、幂等兼容
print("6) 修复前已落库事件保持原样；同请求重试返回原结果，异内容仍冲突")

# 在进程内模拟修复前归一化：省略 seal_intact 落为 false（单事件与替换纠错共用同一函数）
_real_canonical = svc._event_canonical


def _legacy_canonical(data, sample_code=None):
    canon, payload = _real_canonical(data, sample_code)
    if data.event_type == "seal_check" and data.seal_intact is None:
        canon = dict(canon)
        canon["seal_intact"] = False
        payload = {**payload, "seal_intact": False}
    return canon, payload


full_chain_before_check("SD-OLD")
svc._event_canonical = _legacy_canonical
try:
    # 修复前：省略 → 落库 false
    r = ev(check_event("SD-OLD"))
    legacy_created_at = r.json()["event"]["recorded_at"]
    check("模拟修复前：省略落库 false", r.status_code == 201
          and r.json()["event"]["payload"]["seal_intact"] is False, r.text)
    storage("SD-OLD")
    legacy_versions = versions("SD-OLD")["total"]
    legacy_snap = snapshot("SD-OLD")
    check("修复前结论 invalid（历史快照）", legacy_snap["status"] == "invalid", str(legacy_snap["status"]))

    # 修复前的替换纠错（新事件省略 seal_intact，旧 request_hash）
    full_chain_before_check("SD-OLDFIX")
    ev({"event_id": "SD-OLDFIX-C", "event_type": "seal_check", "sample_code": "SD-OLDFIX",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-WRONG", "seal_intact": True})
    legacy_fix_body = {
        "correction_id": "SD-OLDFIX-1", "sample_code": "SD-OLDFIX",
        "target_event_id": "SD-OLDFIX-C", "action": "replace",
        "operator": "纠错员", "reason": "扫码错误",
        "new_event": {"event_id": "SD-OLDFIX-C2", "event_type": "seal_check",
                      "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-X"}}
    check("模拟修复前替换纠错 201", correction(legacy_fix_body).status_code == 201)
    legacy_fix_versions = versions("SD-OLDFIX")["total"]
finally:
    svc._event_canonical = _real_canonical

# ---- 恢复修复后语义：历史保持原样 ----
c = chain("SD-OLD")
check("历史事件 payload 保持 false（不重算）",
      seal_payload("SD-OLD", "SD-OLD-C")["seal_intact"] is False, str(seal_payload("SD-OLD", "SD-OLD-C")))
check("历史链结论仍 invalid", c["status"] == "invalid")
check("历史核定仍被拒绝", approve("SD-OLD").status_code == 422)
check("历史版本数量不变（不补算版本）", versions("SD-OLD")["total"] == legacy_versions)
snap = snapshot("SD-OLD")
check("历史版本快照保持原样（false/invalid）",
      snap["status"] == "invalid"
      and next(e for e in snap["timeline"] if e["event_id"] == "SD-OLD-C")["payload"]["seal_intact"] is False,
      str(snap["status"]))

# 历史同请求（省略）重试 → 200 幂等、返回原结果（原行 false）
r = ev(check_event("SD-OLD"))
check("历史省略重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("幂等返回原行内容（false）与原 recorded_at",
      r.json()["event"]["payload"]["seal_intact"] is False
      and r.json()["event"]["recorded_at"] == legacy_created_at, str(r.json()["event"]))
check("历史重试不产生新版本", versions("SD-OLD")["total"] == legacy_versions)
# 显式 true → 新语义下与旧行异内容 → 409
r = ev({**check_event("SD-OLD"), "seal_intact": True})
check("历史行显式 true 重试 → 409", r.status_code == 409
      and r.json()["error"]["code"] == "event_conflict", r.text)
# 异内容 → 409
r = ev({**check_event("SD-OLD"), "occurred_at": "2026-09-30T03:31:00Z"})
check("历史行异内容重试 → 409", r.status_code == 409, r.text)

# 历史替换纠错：省略重试 200、显式 true/异内容 409、记录与新版本不变
r = correction(legacy_fix_body)
check("历史替换纠错省略重试 200", r.status_code == 200, r.text)
check("历史纠错重试版本不增", versions("SD-OLDFIX")["total"] == legacy_fix_versions)
old_fix_event = next(e for e in chain("SD-OLDFIX")["timeline"] if e["event_id"] == "SD-OLDFIX-C2")
check("历史纠错新事件内容保持 false", old_fix_event["payload"]["seal_intact"] is False,
      str(old_fix_event["payload"]))
r = correction({**legacy_fix_body,
                "new_event": {**legacy_fix_body["new_event"], "seal_intact": True}})
check("历史纠错显式 true 重试 → 409", r.status_code == 409
      and r.json()["error"]["code"] == "correction_conflict", r.text)
r = correction({**legacy_fix_body, "reason": "被篡改的原因"})
check("历史纠错异内容重试 → 409", r.status_code == 409
      and r.json()["error"]["code"] == "correction_conflict", r.text)
check("历史冲突不产生新版本", versions("SD-OLDFIX")["total"] == legacy_fix_versions)

# 历史快照内的纠错新事件同样保持 false
old_fix_snap = snapshot("SD-OLDFIX")
check("历史纠错版本快照保持原样",
      next(e for e in old_fix_snap["timeline"] if e["event_id"] == "SD-OLDFIX-C2")["payload"]["seal_intact"] is False)

# ---------------------------------------------------------------- 7. 启动可用 / HTTP 可调用
print("7) 启动可用：重复 init_db 幂等，HTTP 端点保持可调用")
init_db()  # 已升级库直接沿用，不重算任何历史
check("健康检查 200", client.get("/health").status_code == 200)
check("根信息 200", client.get("/").status_code == 200)
r = client.get(f"{API}/samples/SD-NEW/chain")
check("证据链端点可调用且历史核定样本仍 valid",
      r.status_code == 200 and r.json()["status"] == "valid", r.text)
check("历史样本证据链仍可调用且 invalid",
      client.get(f"{API}/samples/SD-OLD/chain").json()["status"] == "invalid")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
