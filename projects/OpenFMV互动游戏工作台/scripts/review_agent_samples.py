import argparse
import hashlib
import json
import uuid
from copy import deepcopy

from smoke_agents import ROOT, ROLES, load_completed_bundle, read_stored_samples, revision_ancestry, revision_base, save_samples
from agent_smoke_evidence import write_private_json


def compose_current_samples(base_id: str, revision_ids: list[str], selections: list[list[str]]) -> tuple[list[dict], dict, dict]:
    base = load_completed_bundle(base_id, require_full_stage=True)
    bundles = {base["verificationId"]: base}
    for identity in revision_ids:
        identity = str(uuid.UUID(identity))
        if identity in bundles:
            raise ValueError("当前样例的来源证明不能重复列出")
        bundle = load_completed_bundle(identity)
        lineage = bundle["proof"].get("revisionLineage")
        if not isinstance(lineage, dict) or lineage.get("baseVerificationId") != base["verificationId"] or lineage.get("baseProofSha256") != base["proofSha256"]:
            raise ValueError("修订证明没有引用本次原九角色证明")
        bundles[identity] = bundle
    chosen = {role: base["verificationId"] for role in ROLES}
    seen = set()
    for role, identity in selections:
        identity = str(uuid.UUID(identity))
        if role not in ROLES or role in seen or identity not in bundles:
            raise ValueError("每个角色须明确选择一次有效来源证明")
        chosen[role] = identity
        seen.add(role)
    if any(identity not in set(chosen.values()) for identity in bundles if identity != base["verificationId"]):
        raise ValueError("每份修订证明须明确选择其中至少一个角色")
    return _compose_samples(chosen, bundles, {role: base for role in ROLES}, "original_nine_roles", base["verificationId"])


def compose_independent_samples(selections: list[list[str]]) -> tuple[list[dict], dict, dict]:
    if len(selections) != len(ROLES) or {role for role, _ in selections} != set(ROLES):
        raise ValueError("独立来源组合须逐角色明确选择，九个岗位必须恰好齐备且不能重复")
    chosen, bundles, bases = {}, {}, {}
    for role, raw_identity in selections:
        identity = str(uuid.UUID(raw_identity))
        if identity not in bundles:
            bundles[identity] = load_completed_bundle(identity)
        chosen[role] = identity
        bases[role] = revision_base(bundles[identity])
    return _compose_samples({role: chosen[role] for role in ROLES}, bundles, bases, "independent_sources", None)


def _compose_samples(chosen: dict, bundles: dict, bases: dict, mode: str, base_id: str | None) -> tuple[list[dict], dict, dict]:
    if set(chosen) != set(ROLES) or set(bases) != set(ROLES):
        raise ValueError("当前报告必须包含全部九个角色的明确来源和基准")
    samples, verified, manifest_selections = [], {}, {}
    accounting_bundles = dict(bundles)
    for role, identity in chosen.items():
        bundle = bundles[identity]
        base = bases[role]
        original_sample = next((item for item in bundle["samples"] if item["role"] == role), None)
        if original_sample is None:
            raise ValueError("所选证明没有对应角色的完整专业产物")
        sample = deepcopy(original_sample)
        actual = bundle["verified"][sample["runId"]]
        delivery = actual["delivery"]
        for ancestor_id, ancestor in revision_ancestry(bundle, base, role).items():
            previous = accounting_bundles.get(ancestor_id)
            if previous is not None and previous["proofSha256"] != ancestor["proofSha256"]:
                raise ValueError("同一来源证明在只读组合期间发生变化")
            accounting_bundles[ancestor_id] = ancestor
        write = actual["writeReceipt"]
        call_id = write["operationId"].removeprefix("agent-")
        call = next(item for item in bundle["proof"]["calls"] if item["callId"] == call_id and item["runId"] == sample["runId"])
        if call["result"] != "通过" or not call["structureValid"]:
            raise ValueError("当前版本缺少真实通过的专业产物写入调用")
        run_calls = [item for item in bundle["proof"]["calls"] if item["runId"] == sample["runId"]]
        selection = {"sourceProofId": identity, "sourceProofPath": str((bundle["directory"] / "proof.json").relative_to(ROOT)),
            "sourceProofSha256": bundle["proofSha256"], "sourceRunId": sample["runId"], "sourceProjectId": sample["projectId"],
            "sourceProofFullStageCoverage": bundle["proof"].get("fullStageCoverage"),
            "baseProofId": base["verificationId"], "baseProofSha256": base["proofSha256"],
            "sourceTargetId": sample["targetId"], "artifactSha256": delivery["artifactSha256"], "contentSha256": delivery["contentSha256"],
            "writeCallId": call_id, "writeOperationId": write["operationId"], "writeCallInputSha256": call["inputSha256"], "writeCallOutputSha256": call["outputSha256"],
            "sourceRunCallIds": [item["callId"] for item in run_calls], "sourceRunCostMicros": sum(item["costMicros"] for item in run_calls),
            "contentReviewStatus": "待本人内容评分"}
        sample["currentSelection"] = selection
        samples.append(sample)
        verified[sample["runId"]] = actual
        manifest_selections[role] = selection
    calls, request_ids, proof_sources = {}, {}, []
    for identity, bundle in accounting_bundles.items():
        proof = bundle["proof"]
        if any(call["costMicros"] is None for call in proof["calls"]) or sum(call["costMicros"] for call in proof["calls"]) != proof["accounting"]["spentMicros"]:
            raise ValueError("来源证明的全部实际调用费用尚未完整核对")
        for call in proof["calls"]:
            if call["callId"] in calls and calls[call["callId"]] != call:
                raise ValueError("同一实际调用在多个来源证明中存在差异")
            request = (call["provider"], call["requestId"])
            if call["requestId"] and request in request_ids and request_ids[request] != call["callId"]:
                raise ValueError("同一真实供应商请求不能重复计入样例费用")
            calls[call["callId"]] = call
            if call["requestId"]:
                request_ids[request] = call["callId"]
        proof_sources.append({"verificationId": identity, "proofSha256": bundle["proofSha256"], "completed": proof["completed"], "restartVerified": proof["restartVerified"],
            "fullStageCoverage": proof.get("fullStageCoverage"), "selectedRoles": proof.get("selectedRoles"),
            "callIds": [call["callId"] for call in proof["calls"]], "costMicros": proof["accounting"]["spentMicros"]})
    manifest = {"schemaVersion": 1, "compositionMode": mode, "baseVerificationId": base_id, "fullRoleCoverage": True, "roleCount": len(samples),
        "roleBaselines": {role: {"verificationId": bases[role]["verificationId"], "proofSha256": bases[role]["proofSha256"],
            "fullStageCoverage": bases[role]["proof"].get("fullStageCoverage")} for role in ROLES},
        "automaticValidationScope": "专业结构、真实来源、实际工具回执与持久保存核验", "automaticValidationPassed": True,
        "contentReviewStatus": "待本人内容评分", "contentAccepted": False, "selections": manifest_selections, "sourceProofs": proof_sources,
        "actualCalls": list(calls.values()), "actualCallCount": len(calls), "totalCostMicros": sum(call["costMicros"] for call in calls.values()),
        "costScope": "本报告列出的全部来源证明及修订祖先，包含各证明的全部实际调用；成本单位为微元人民币",
        "sourceFilesUnchanged": True}
    return samples, verified, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="只读整理已有真实角色产物供本人评分，不调用模型或改变任务状态")
    parser.add_argument("--verification-id", help="原报告或原九角色组合模式使用的证明ID")
    parser.add_argument("--current", action="store_true", help="从完整九角色证明与明确选择的修订证明生成当前九角色样例和manifest")
    parser.add_argument("--independent-sources", action="store_true", help="九个角色各自明确选择完整通过的独立来源，修订沿各自基准核验")
    parser.add_argument("--revision-id", action="append", default=[], help="完整验证通过且关联原九角色证明的修订证明，可重复提供")
    parser.add_argument("--select", nargs=2, action="append", default=[], metavar=("ROLE", "PROOF_ID"), help="明确选择角色来源；独立来源模式须逐项提供全部九个角色，原九角色模式其余角色沿用基准")
    arguments = parser.parse_args()
    if arguments.independent_sources:
        if arguments.verification_id or arguments.current or arguments.revision_id:
            parser.error("独立来源模式仅使用--independent-sources和九项--select，不混用原九角色组合参数")
        identity = None
        directory = ROOT / ".intermediate" / "agent-smoke"
        samples, verified, manifest = compose_independent_samples(arguments.select)
    else:
        if not arguments.verification_id:
            parser.error("请选择--verification-id或--independent-sources模式")
        identity = str(uuid.UUID(arguments.verification_id))
        directory = ROOT / ".intermediate" / "agent-smoke" / identity
        current = arguments.current or arguments.revision_id or arguments.select
        if current:
            samples, verified, manifest = compose_current_samples(identity, arguments.revision_id, arguments.select)
        else:
            samples = json.loads((directory / "requests.json").read_text(encoding="utf-8"))
            verified = read_stored_samples(directory, samples)
            manifest = None
    output = directory / "reviews" / str(uuid.uuid4())
    hashes = save_samples(output, samples, verified, manifest)
    if manifest is not None:
        write_private_json(output / "manifest.json", {**manifest, "sampleFiles": hashes})
        hashes["manifest.json"] = hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    print(json.dumps({"verificationId": identity, "samples": str((output / "samples.html").relative_to(ROOT)), "sampleCount": len(samples), "sha256": hashes, "readonlySource": True,
        **({"manifest": str((output / "manifest.json").relative_to(ROOT)), "actualCallCount": manifest["actualCallCount"], "totalCostMicros": manifest["totalCostMicros"], "contentAccepted": False} if manifest is not None else {})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
