#!/usr/bin/env python3
"""PC Bridge server linking Antigravity Mobile App with Antigravity.exe."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import mimetypes
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ANTIGRAVITY_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "antigravity", "Antigravity.exe")
ANTIGRAVITY_DATA_DIR = os.path.expanduser(r"~\.gemini\antigravity")
DAEMON_DIR = os.path.join(ANTIGRAVITY_DATA_DIR, "daemon")
SUMMARIES_DB = os.path.join(ANTIGRAVITY_DATA_DIR, "conversation_summaries.db")
APP_STORAGE_PATH = os.path.join(os.environ.get("APPDATA", ""), "Antigravity", "app_storage.json")
CONVERSATIONS_DIR = os.path.join(ANTIGRAVITY_DATA_DIR, "conversations")
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_PORT = 18765
BRIDGE_VERSION = "1.1.0"

SKIP_STEP_TYPES = {
    "CORTEX_STEP_TYPE_CHECKPOINT",
    "CORTEX_STEP_TYPE_DUMMY",
    "CORTEX_STEP_TYPE_EPHEMERAL_MESSAGE",
    "CORTEX_STEP_TYPE_CONVERSATION_HISTORY",
    "CORTEX_STEP_TYPE_KNOWLEDGE_ARTIFACTS",
    "CORTEX_STEP_TYPE_DIRECTORY_RULES",
}


def get_lan_ips() -> list[str]:
    """Return non-loopback IPv4 addresses on this host."""
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.insert(0, ip)
    except OSError:
        pass
    return ips


def _list_process_pids(image_name: str) -> set[int]:
    """List live Windows PIDs for a given executable image name."""
    pids: set[int] = set()
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("INFO:"):
                continue
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == image_name.lower():
                try:
                    pids.add(int(parts[1]))
                except ValueError:
                    pass
    except Exception:
        pass
    return pids


class AntigravityHubClient:
    """Discovers and communicates with the live language_server.exe owned by Antigravity.exe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hub: dict[str, Any] | None = None

    def is_antigravity_running(self) -> bool:
        return bool(_list_process_pids("Antigravity.exe"))

    def _probe_hub(self, http_port: int, csrf_token: str) -> bool:
        url = f"http://127.0.0.1:{http_port}/exa.language_server_pb.LanguageServerService/GetStatus"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": csrf_token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def discover_hub(self, force: bool = False) -> dict[str, Any] | None:
        with self._lock:
            if self._hub and not force:
                if self._probe_hub(self._hub["httpPort"], self._hub["csrfToken"]):
                    return dict(self._hub)
                self._hub = None

            live_ls_pids = _list_process_pids("language_server.exe")
            if not live_ls_pids:
                self._hub = None
                return None

            files = glob.glob(os.path.join(DAEMON_DIR, "hub_*.json"))
            files.sort(key=os.path.getmtime, reverse=True)
            for path in files:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        info = json.load(f)
                    pid = int(info.get("pid", 0))
                    http_port = int(info.get("httpPort", 0))
                    csrf = str(info.get("csrfToken", ""))
                    if pid in live_ls_pids and http_port > 0 and csrf:
                        if self._probe_hub(http_port, csrf):
                            info["hubFile"] = os.path.basename(path)
                            self._hub = info
                            return dict(info)
                except Exception:
                    continue
            return None

    def rpc(self, method: str, payload: dict[str, Any] | None = None, timeout: float = 20.0) -> dict[str, Any]:
        hub = self.discover_hub()
        if not hub:
            raise RuntimeError("Antigravity.exe 未运行或 language_server 未就绪")
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(2):
            if attempt > 0:
                hub = self.discover_hub(force=True)
                if not hub:
                    break
            url = f"http://127.0.0.1:{hub['httpPort']}/exa.language_server_pb.LanguageServerService/{method}"
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-codeium-csrf-token": hub["csrfToken"],
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                if e.code in (401, 403, 412) and attempt == 0:
                    last_err = e
                    continue
                raise RuntimeError(f"Antigravity RPC {method} HTTP {e.code}: {err_body}") from e
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Antigravity RPC {method} 失败: {last_err}")

    def launch_antigravity(self, wait_seconds: float = 20.0) -> dict[str, Any]:
        if not self.is_antigravity_running():
            if not os.path.exists(ANTIGRAVITY_EXE):
                raise FileNotFoundError(f"找不到 {ANTIGRAVITY_EXE}")
            subprocess.Popen(
                [ANTIGRAVITY_EXE],
                cwd=os.path.dirname(ANTIGRAVITY_EXE),
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                close_fds=True,
            )
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            hub = self.discover_hub(force=True)
            if hub:
                return hub
            time.sleep(0.5)
        raise TimeoutError("已启动 Antigravity.exe，但等待 language_server 就绪超时")


def _extract_tool_summary(step: dict[str, Any]) -> tuple[str, str, str]:
    """Return (tool_name, short_title, detail_output) for a tool/generic step."""
    st_type = step.get("type", "")
    meta = step.get("metadata") or {}
    tool_call = meta.get("toolCall") or {}
    tc_name = tool_call.get("name", "")
    args_str = tool_call.get("argumentsJson", "")
    args_obj: dict[str, Any] = {}
    if isinstance(args_str, str) and args_str.strip().startswith("{"):
        try:
            args_obj = json.loads(args_str)
        except Exception:
            args_obj = {}

    if st_type == "CORTEX_STEP_TYPE_RUN_COMMAND":
        rc = step.get("runCommand") or {}
        cmd = rc.get("commandLine") or rc.get("proposedCommandLine") or args_obj.get("CommandLine") or ""
        cwd = rc.get("cwd") or ""
        out_obj = rc.get("combinedOutput") or rc.get("combinedOutputSnapshot") or {}
        out_txt = out_obj.get("output") or rc.get("stdout") or ""
        exit_code = rc.get("exitCode")
        header = f"$ {cmd}" if cmd else "执行命令"
        parts = []
        if cwd:
            parts.append(f"目录: {cwd}")
        if exit_code is not None:
            parts.append(f"退出码: {exit_code}")
        if out_txt:
            parts.append(out_txt[:3500])
        return "终端命令", header[:140], "\n".join(parts)

    if st_type == "CORTEX_STEP_TYPE_VIEW_FILE":
        vf = step.get("viewFile") or {}
        path = vf.get("absolutePathUri") or args_obj.get("Path") or ""
        return "读取文件", f"读取 {os.path.basename(path) or path}"[:120], path

    if st_type == "CORTEX_STEP_TYPE_CODE_ACTION":
        ca = step.get("codeAction") or {}
        desc = ca.get("description") or args_obj.get("Path") or args_obj.get("TargetFile") or "编辑文件"
        return "代码修改", str(desc)[:120], json.dumps(args_obj, ensure_ascii=False)[:1500]

    if st_type == "CORTEX_STEP_TYPE_MCP_TOOL":
        mcp = step.get("mcpTool") or {}
        srv = mcp.get("serverName") or args_obj.get("ServerName") or ""
        tname = mcp.get("toolName") or args_obj.get("ToolName") or tc_name or "MCP"
        return f"MCP · {srv}" if srv else "MCP 工具", str(tname)[:120], json.dumps(args_obj, ensure_ascii=False)[:1500]

    if st_type == "CORTEX_STEP_TYPE_GENERIC":
        gen = step.get("generic") or {}
        render = (gen.get("result") or {}).get("stepRenderInfo") or {}
        prefix = render.get("titlePrefix") or ""
        title_parts = " ".join(p.get("text", "") for p in (render.get("titleParts") or []) if isinstance(p, dict))
        title = f"{prefix} {title_parts}".strip() or tc_name or "操作"
        blocks = render.get("contentBlocks") or []
        detail_lines: list[str] = []
        for b in blocks:
            if isinstance(b, dict):
                if "terminal" in b:
                    term = b["terminal"]
                    if term.get("command"):
                        detail_lines.append(f"$ {term['command']}")
                    if term.get("output"):
                        detail_lines.append(str(term["output"])[:3000])
                elif "text" in b:
                    detail_lines.append(str(b["text"])[:2000])
        if not detail_lines and args_obj:
            detail_lines.append(json.dumps(args_obj, ensure_ascii=False)[:1500])
        return tc_name or render.get("icon") or "工具", title[:140], "\n".join(detail_lines)

    short_type = st_type.replace("CORTEX_STEP_TYPE_", "").lower()
    summary = tc_name or short_type
    if args_obj:
        first_val = next(iter(args_obj.values()), "")
        if isinstance(first_val, str) and first_val:
            summary = f"{summary}: {first_val[:90]}"
    return tc_name or short_type, summary[:140], json.dumps(args_obj, ensure_ascii=False)[:1500]


def _is_suppressed_error_step(step: dict[str, Any] | None) -> bool:
    """Return True for internal/transient ERROR_MESSAGE steps hidden in the PC UI."""
    if not step or step.get("type") != "CORTEX_STEP_TYPE_ERROR_MESSAGE":
        return False
    em = step.get("errorMessage") or {}
    return not bool(em.get("shouldShowUser"))


def normalize_step(
    idx: int,
    step: dict[str, Any],
    trajectory_id: str,
    next_step: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Convert raw Cortex Step into a mobile-friendly timeline item."""
    st_type = step.get("type", "")
    if st_type in SKIP_STEP_TYPES:
        return None

    status = step.get("status", "CORTEX_STEP_STATUS_UNSPECIFIED").replace("CORTEX_STEP_STATUS_", "")
    meta = step.get("metadata") or {}
    timestamp = meta.get("completedAt") or meta.get("createdAt") or ""
    model = meta.get("generatorModel") or (meta.get("modelUsage") or {}).get("model") or ""

    pending_interaction: dict[str, Any] | None = None
    if status == "WAITING":
        if st_type == "CORTEX_STEP_TYPE_RUN_COMMAND":
            rc = step.get("runCommand") or {}
            pending_interaction = {
                "kind": "run_command",
                "trajectoryId": trajectory_id,
                "stepIndex": idx,
                "title": "确认运行终端命令",
                "commandLine": rc.get("proposedCommandLine") or rc.get("commandLine") or "",
                "cwd": rc.get("cwd") or "",
            }
        elif st_type == "CORTEX_STEP_TYPE_ASK_QUESTION":
            aq = step.get("askQuestion") or {}
            pending_interaction = {
                "kind": "ask_question",
                "trajectoryId": trajectory_id,
                "stepIndex": idx,
                "title": "需要你的选择",
                "questions": aq.get("questions") or [],
            }
        else:
            pending_interaction = {
                "kind": "generic",
                "trajectoryId": trajectory_id,
                "stepIndex": idx,
                "title": f"等待用户确认 ({st_type.replace('CORTEX_STEP_TYPE_', '')})",
            }

    if st_type == "CORTEX_STEP_TYPE_USER_INPUT":
        ui = step.get("userInput") or {}
        parts = []
        for item in ui.get("items") or []:
            if isinstance(item, dict):
                if item.get("text"):
                    parts.append(item["text"])
                elif item.get("item"):
                    parts.append(f"@{json.dumps(item['item'], ensure_ascii=False)}")
        text = "".join(parts).strip() or (ui.get("userResponse") or "").strip()
        media_items = []
        for m in ui.get("media") or []:
            if isinstance(m, dict):
                uri = str(m.get("uri") or "")
                dname = str(m.get("displayName") or m.get("description") or (os.path.basename(uri) if uri else "附件"))
                mime = str(m.get("mimeType") or mimetypes.guess_type(dname or uri)[0] or "application/octet-stream")
                media_items.append({
                    "displayName": dname,
                    "mimeType": mime,
                    "uri": uri,
                })
        if not text and not media_items:
            return None
        return {
            "id": f"step-{idx}",
            "stepIndex": idx,
            "role": "user",
            "stepType": st_type,
            "status": status,
            "timestamp": timestamp,
            "text": text,
            "media": media_items,
            "thinking": "",
            "toolCalls": [],
            "toolName": "",
            "toolSummary": "",
            "toolOutput": "",
            "model": model,
            "pendingInteraction": pending_interaction,
        }

    if st_type == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
        pr = step.get("plannerResponse") or {}
        text = (pr.get("modifiedResponse") or pr.get("response") or "").strip()
        thinking = (pr.get("thinking") or "").strip()
        raw_tcs = pr.get("toolCalls") or []
        tcs = [
            {"id": tc.get("id", ""), "name": tc.get("name", "")}
            for tc in raw_tcs
            if isinstance(tc, dict) and tc.get("name")
        ]
        if not text and not thinking and not tcs:
            return None
        if status == "DONE" and not text and not tcs and _is_suppressed_error_step(next_step):
            return None
        return {
            "id": pr.get("messageId") or f"step-{idx}",
            "stepIndex": idx,
            "role": "assistant",
            "stepType": st_type,
            "status": status,
            "timestamp": timestamp,
            "text": text,
            "thinking": thinking,
            "toolCalls": tcs,
            "toolName": "",
            "toolSummary": "",
            "toolOutput": "",
            "model": model,
            "pendingInteraction": pending_interaction,
        }

    if st_type == "CORTEX_STEP_TYPE_NOTIFY_USER":
        nu = step.get("notifyUser") or {}
        text = (nu.get("notificationContent") or "").strip()
        if not text:
            return None
        return {
            "id": f"step-{idx}",
            "stepIndex": idx,
            "role": "assistant",
            "stepType": st_type,
            "status": status,
            "timestamp": timestamp,
            "text": text,
            "thinking": "",
            "toolCalls": [],
            "toolName": "",
            "toolSummary": "",
            "toolOutput": "",
            "model": model,
            "pendingInteraction": pending_interaction,
        }

    if st_type == "CORTEX_STEP_TYPE_ERROR_MESSAGE":
        if _is_suppressed_error_step(step):
            return None
        err = (step.get("errorMessage") or {}).get("error") or {}
        msg = err.get("userErrorMessage") or err.get("shortError") or err.get("fullError") or "发生未知错误"
        return {
            "id": f"step-{idx}",
            "stepIndex": idx,
            "role": "error",
            "stepType": st_type,
            "status": status,
            "timestamp": timestamp,
            "text": str(msg),
            "thinking": "",
            "toolCalls": [],
            "toolName": "错误",
            "toolSummary": str(msg)[:120],
            "toolOutput": str(err.get("fullError") or ""),
            "model": model,
            "pendingInteraction": pending_interaction,
        }

    if st_type == "CORTEX_STEP_TYPE_TASK_BOUNDARY":
        tb = step.get("taskBoundary") or {}
        task_name = tb.get("taskName") or tb.get("taskSummary") or ""
        if not task_name:
            return None
        return {
            "id": f"step-{idx}",
            "stepIndex": idx,
            "role": "tool",
            "stepType": st_type,
            "status": status,
            "timestamp": timestamp,
            "text": "",
            "thinking": "",
            "toolCalls": [],
            "toolName": "任务阶段",
            "toolSummary": f"{tb.get('mode', '')} · {task_name}".strip(" ·"),
            "toolOutput": tb.get("taskStatus") or "",
            "model": model,
            "pendingInteraction": pending_interaction,
        }

    if st_type == "CORTEX_STEP_TYPE_SYSTEM_MESSAGE":
        return None

    tool_name, tool_summary, tool_output = _extract_tool_summary(step)
    return {
        "id": f"step-{idx}",
        "stepIndex": idx,
        "role": "tool",
        "stepType": st_type,
        "status": status,
        "timestamp": timestamp,
        "text": "",
        "thinking": "",
        "toolCalls": [],
        "toolName": tool_name,
        "toolSummary": tool_summary,
        "toolOutput": tool_output,
        "model": model,
        "pendingInteraction": pending_interaction,
    }


def build_cascade_config(model_id: str | None = None, auto_exec_policy: str | None = None) -> dict[str, Any]:
    policy = auto_exec_policy or "CASCADE_COMMANDS_AUTO_EXECUTION_AUTO"
    planner: dict[str, Any] = {
        "supportsLatexRendering": True,
        "toolConfig": {
            "runCommand": {
                "autoCommandConfig": {
                    "autoExecutionPolicy": policy,
                }
            },
            "notifyUser": {
                "artifactReviewMode": "ARTIFACT_REVIEW_MODE_AUTO",
            },
        },
    }
    if model_id:
        planner["planModel"] = model_id
    return {
        "plannerConfig": planner,
        "conversationHistoryConfig": {},
        "executorConfig": {"useCoreDirect": True},
    }


class AntigravityBridgeService:
    def _get_pinned_conversation_ids(self) -> list[str]:
        if not os.path.exists(APP_STORAGE_PATH):
            return []
        try:
            with open(APP_STORAGE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            pinned = data.get("pinned_conversations_order") or []
            if isinstance(pinned, str):
                pinned = json.loads(pinned)
            if isinstance(pinned, list):
                return [str(x) for x in pinned if x]
        except Exception:
            pass
        return []

    """High-level orchestration over Antigravity language_server RPC and SQLite history."""

    def __init__(self, hub_client: AntigravityHubClient) -> None:
        self.hub = hub_client
        self._default_model_cache: str | None = None
        self._user_cache: tuple[str, str, float] = ("", "", 0.0)

    def get_status(self, host_port: int) -> dict[str, Any]:
        running = self.hub.is_antigravity_running()
        hub_info = self.hub.discover_hub()
        user_name, user_email, cached_at = self._user_cache
        default_model = self._default_model_cache or "MODEL_PLACEHOLDER_M318"
        if hub_info and (time.time() - cached_at > 120 or not self._default_model_cache):
            try:
                us = self.hub.rpc("GetUserStatus", {}, timeout=1.5)
                user_status = us.get("userStatus") or {}
                user_name = user_status.get("name", "")
                user_email = user_status.get("email", "")
                self._user_cache = (user_name, user_email, time.time())
            except Exception:
                pass
            try:
                mc = self.hub.rpc("GetCascadeModelConfigData", {}, timeout=1.5)
                dm = ((mc.get("defaultOverrideModelConfig") or {}).get("modelOrAlias") or {}).get("model")
                if dm:
                    default_model = dm
                    self._default_model_cache = dm
            except Exception:
                pass
        return {
            "ok": True,
            "bridgeVersion": BRIDGE_VERSION,
            "hostIps": get_lan_ips(),
            "bridgePort": host_port,
            "antigravityExePath": ANTIGRAVITY_EXE,
            "antigravityRunning": running,
            "hubConnected": hub_info is not None,
            "hubPort": hub_info.get("httpPort") if hub_info else None,
            "hubPid": hub_info.get("pid") if hub_info else None,
            "lsVersion": hub_info.get("lsVersion") if hub_info else None,
            "defaultModel": default_model,
            "userName": user_name,
            "userEmail": user_email,
        }

    def get_models(self) -> dict[str, Any]:
        mc = self.hub.rpc("GetCascadeModelConfigData", {})
        default_model = ((mc.get("defaultOverrideModelConfig") or {}).get("modelOrAlias") or {}).get("model") or ""
        if default_model:
            self._default_model_cache = default_model
        models = []
        for cfg in mc.get("clientModelConfigs") or []:
            moa = cfg.get("modelOrAlias") or {}
            mid = moa.get("model") or moa.get("alias") or ""
            if not mid:
                continue
            qi = cfg.get("quotaInfo") or {}
            models.append({
                "id": mid,
                "label": cfg.get("label") or mid,
                "supportsImages": bool(cfg.get("supportsImages")),
                "remainingFraction": qi.get("remainingFraction"),
                "resetTime": qi.get("resetTime") or "",
                "isDefault": mid == default_model,
            })
        return {"defaultModel": default_model, "models": models}

    def get_workspaces(self) -> dict[str, Any]:
        res: dict[str, Any] = {"homeDirPath": "", "homeDirUri": "", "projects": []}
        try:
            wi = self.hub.rpc("GetWorkspaceInfos", {})
            res["homeDirPath"] = wi.get("homeDirPath", "")
            res["homeDirUri"] = wi.get("homeDirUri", "")
        except Exception:
            pass
        try:
            lp = self.hub.rpc("ListProjects", {})
            res["projects"] = lp.get("projectIds") or []
        except Exception:
            pass
        return res

    def _read_sqlite_summaries(self) -> list[dict[str, Any]]:
        if not os.path.exists(SUMMARIES_DB):
            return []
        rows_out: list[dict[str, Any]] = []
        try:
            conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=3.0)
            try:
                cur = conn.execute(
                    """
                    SELECT conversation_id, title, preview, step_count, last_modified_time,
                           workspace_uris, status, source, project_id, last_user_input_time
                    FROM conversation_summaries
                    WHERE parent_conversation_id = '' AND nesting_depth = 0
                    ORDER BY last_modified_time DESC
                    LIMIT 200
                    """
                )
                for cid, title, preview, step_count, mtime, ws_json, status, source, project_id, u_time in cur:
                    db_path = os.path.join(CONVERSATIONS_DIR, f"{cid}.db")
                    if not os.path.exists(db_path):
                        continue
                    if source == "CORTEX_TRAJECTORY_SOURCE_CLI":
                        continue
                    ws_list: list[str] = []
                    if ws_json:
                        try:
                            parsed = json.loads(ws_json)
                            if isinstance(parsed, list):
                                ws_list = [str(x) for x in parsed]
                        except Exception:
                            pass
                    rows_out.append({
                        "cascadeId": cid,
                        "title": title or preview or cid[:8],
                        "preview": preview or title or "",
                        "stepCount": int(step_count or 0),
                        "lastModifiedTime": str(mtime or ""),
                        "lastUserInputTime": str(u_time or ""),
                        "status": str(status or "CASCADE_RUN_STATUS_IDLE").replace("CASCADE_RUN_STATUS_", ""),
                        "workspaceUris": ws_list,
                        "projectId": project_id or "",
                        "source": source or "ARCHIVE",
                        "isLive": False,
                    })
            finally:
                conn.close()
        except Exception:
            pass
        return rows_out

    def list_conversations(self, query: str = "", include_archive: bool = True) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        try:
            resp = self.hub.rpc("GetAllCascadeTrajectories", {"excludeSubtrajectories": True})
            summaries = resp.get("trajectorySummaries") or {}
            for cid, info in summaries.items():
                ws = []
                for w in info.get("workspaces") or []:
                    uri = w.get("workspaceFolderAbsoluteUri") or w.get("gitRootAbsoluteUri") or ""
                    if uri:
                        ws.append(uri)
                ann = info.get("annotations") or {}
                title = ann.get("title") or info.get("summary") or cid[:8]
                merged[cid] = {
                    "cascadeId": cid,
                    "title": title,
                    "preview": info.get("summary") or title,
                    "stepCount": int(info.get("stepCount") or 0),
                    "lastModifiedTime": info.get("lastModifiedTime") or info.get("createdTime") or "",
                    "lastUserInputTime": info.get("lastUserInputTime") or "",
                    "status": str(info.get("status") or "CASCADE_RUN_STATUS_IDLE").replace("CASCADE_RUN_STATUS_", ""),
                    "workspaceUris": ws,
                    "projectId": "",
                    "source": "CASCADE_CLIENT",
                    "isLive": True,
                }
        except Exception:
            pass

        if include_archive:
            for item in self._read_sqlite_summaries():
                cid = item["cascadeId"]
                if cid not in merged:
                    merged[cid] = item

        items = list(merged.values())
        q = query.strip().lower()
        if q:
            items = [
                it
                for it in items
                if q in it["title"].lower()
                or q in it["preview"].lower()
                or q in it["cascadeId"].lower()
                or any(q in w.lower() for w in it["workspaceUris"])
            ]
        pinned_ids = self._get_pinned_conversation_ids()
        pinned_set = set(pinned_ids)
        for it in items:
            it["isPinned"] = it["cascadeId"] in pinned_set

        items.sort(key=lambda x: x.get("lastModifiedTime") or "", reverse=True)
        return items

    def get_conversation(self, cascade_id: str, max_steps: int = 400) -> dict[str, Any]:
        resp = self.hub.rpc(
            "GetCascadeTrajectory",
            {
                "cascadeId": cascade_id,
                "trajectoryVerbosity": "CLIENT_TRAJECTORY_VERBOSITY_PROD_UI",
            },
        )
        traj = resp.get("trajectory") or {}
        traj_id = traj.get("trajectoryId") or ""
        steps = list(traj.get("steps") or [])
        num_total = int(resp.get("numTotalSteps") or len(steps))
        while len(steps) < num_total:
            page = self.hub.rpc(
                "GetCascadeTrajectorySteps",
                {
                    "cascadeId": cascade_id,
                    "stepOffset": len(steps),
                    "trajectoryVerbosity": "CLIENT_TRAJECTORY_VERBOSITY_PROD_UI",
                },
            )
            more = page.get("steps") or []
            if not more:
                break
            steps.extend(more)

        start_idx = max(0, len(steps) - max_steps) if max_steps > 0 else 0
        messages: list[dict[str, Any]] = []
        pending_interaction: dict[str, Any] | None = None
        for idx in range(start_idx, len(steps)):
            next_step = steps[idx + 1] if idx + 1 < len(steps) else None
            norm = normalize_step(idx, steps[idx], traj_id, next_step=next_step)
            if norm:
                norm["completedAt"] = str(((steps[idx].get("metadata") or {}).get("completedAt")) or "")
                if norm.get("pendingInteraction"):
                    pending_interaction = norm["pendingInteraction"]
                messages.append(norm)

        title = ""
        title_cache = getattr(self, "_title_cache", None)
        if title_cache is None:
            title_cache = {}
            self._title_cache = title_cache
        cached_title = title_cache.get(cascade_id)
        if cached_title and (time.monotonic() - cached_title[0]) < 10.0:
            title = cached_title[1]
        else:
            try:
                all_t = self.hub.rpc("GetAllCascadeTrajectories", {"excludeSubtrajectories": True})
                info = (all_t.get("trajectorySummaries") or {}).get(cascade_id) or {}
                title = (info.get("annotations") or {}).get("title") or info.get("summary") or ""
                if title:
                    title_cache[cascade_id] = (time.monotonic(), title)
            except Exception:
                if cached_title:
                    title = cached_title[1]
        if not title:
            for m in messages:
                if m["role"] == "user" and m["text"]:
                    title = m["text"][:40]
                    break

        status_raw = resp.get("status") or "CASCADE_RUN_STATUS_IDLE"
        status_str = str(status_raw).replace("CASCADE_RUN_STATUS_", "")
        if status_str == "IDLE" and any(
            m.get("status") in ("RUNNING", "GENERATING", "PENDING")
            for m in messages[-30:]
        ):
            status_str = "RUNNING"
        return {
            "cascadeId": cascade_id,
            "trajectoryId": traj_id,
            "title": title or cascade_id[:8],
            "status": status_str,
            "numTotalSteps": num_total,
            "truncated": start_idx > 0,
            "pendingInteraction": pending_interaction,
            "messages": messages,
        }

    def _resolve_default_model(self) -> str:
        if self._default_model_cache:
            return self._default_model_cache
        try:
            mc = self.hub.rpc("GetCascadeModelConfigData", {})
            dm = ((mc.get("defaultOverrideModelConfig") or {}).get("modelOrAlias") or {}).get("model")
            if dm:
                self._default_model_cache = dm
                return dm
        except Exception:
            pass
        return "MODEL_PLACEHOLDER_M318"

    def start_and_send(
        self,
        message: str,
        model_id: str | None = None,
        workspace_uri: str | None = None,
        auto_exec_policy: str | None = None,
        media: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        cascade_id = str(uuid.uuid4())
        chosen_model = model_id or self._resolve_default_model()
        start_req: dict[str, Any] = {
            "cascadeId": cascade_id,
            "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
            "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
            "requestedModel": chosen_model,
        }
        if workspace_uri:
            start_req["workspaceUris"] = [workspace_uri]
        start_resp = self.hub.rpc("StartCascade", start_req)
        actual_id = start_resp.get("cascadeId") or cascade_id
        self.send_message(
            actual_id,
            message,
            model_id=chosen_model,
            auto_exec_policy=auto_exec_policy,
            media=media,
        )
        return {"cascadeId": actual_id, "status": "sent"}

    def send_message(
        self,
        cascade_id: str,
        message: str,
        model_id: str | None = None,
        auto_exec_policy: str | None = None,
        media: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        chosen_model = model_id or self._resolve_default_model()
        media_payload: list[dict[str, Any]] = []
        for m in media or []:
            if not isinstance(m, dict):
                continue
            entry: dict[str, Any] = {
                "mimeType": str(m.get("mimeType") or "application/octet-stream"),
                "displayName": str(m.get("displayName") or m.get("description") or "attachment"),
                "description": str(m.get("description") or m.get("displayName") or "attachment"),
            }
            if m.get("inlineData"):
                entry["inlineData"] = str(m["inlineData"])
            elif m.get("uri"):
                entry["uri"] = str(m["uri"]).replace("\\", "/")
            else:
                continue
            media_payload.append(entry)

        req: dict[str, Any] = {
            "cascadeId": cascade_id,
            "items": [{"text": message or "请查看并分析我上传的附件"}],
            "cascadeConfig": build_cascade_config(chosen_model, auto_exec_policy),
        }
        if media_payload:
            req["media"] = media_payload
        self.hub.rpc("SendUserCascadeMessage", req)
        return {"cascadeId": cascade_id, "status": "sent"}

    def cancel(self, cascade_id: str) -> dict[str, Any]:
        self.hub.rpc(
            "CancelCascadeInvocation",
            {"cascadeId": cascade_id, "killBackgroundTasks": True},
        )
        return {"cascadeId": cascade_id, "status": "canceled"}

    def delete(self, cascade_id: str) -> dict[str, Any]:
        self.hub.rpc("DeleteCascadeTrajectory", {"cascadeId": cascade_id})
        return {"cascadeId": cascade_id, "status": "deleted"}

    def interact(self, cascade_id: str, body: dict[str, Any]) -> dict[str, Any]:
        trajectory_id = body.get("trajectoryId") or ""
        step_index = int(body.get("stepIndex", 0))
        kind = body.get("kind", "run_command")
        confirm = bool(body.get("confirm", True))

        interaction: dict[str, Any] = {
            "trajectoryId": trajectory_id,
            "stepIndex": step_index,
        }
        if kind == "run_command":
            cmd = str(body.get("commandLine") or "")
            interaction["runCommand"] = {
                "confirm": confirm,
                "proposedCommandLine": cmd,
                "submittedCommandLine": cmd,
            }
        elif kind == "permission":
            interaction["permission"] = {"allow": confirm, "scope": "PERMISSION_SCOPE_ONCE"}
        elif kind == "file_permission":
            interaction["filePermission"] = {"allow": confirm}
        elif kind == "ask_question":
            interaction["askQuestion"] = body.get("askQuestion") or {}
        else:
            interaction["approvalInteraction"] = {"confirm": confirm}

        self.hub.rpc(
            "HandleCascadeUserInteraction",
            {"cascadeId": cascade_id, "interaction": interaction},
        )
        return {"cascadeId": cascade_id, "status": "interacted"}


class BridgeHandler(BaseHTTPRequestHandler):
    service: AntigravityBridgeService
    bridge_port: int
    auth_token: str

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        ts = time.strftime("%H:%M:%S")
        stream = sys.stderr
        if stream is not None:
            try:
                stream.write(f"[{ts}] {self.address_string()} {format % args}\n")
                stream.flush()
            except Exception:
                pass

    def _check_auth(self) -> bool:
        if not self.auth_token:
            return True
        got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not got:
            got = self.headers.get("X-Bridge-Token", "").strip()
        if got == self.auth_token:
            return True
        self._send_json(401, {"error": "Unauthorized"})
        return False

    def _send_json(self, code: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Bridge-Token, X-File-Name")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Bridge-Token, X-File-Name")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_media_get(self, uri: str) -> None:
        if not uri:
            self._send_json(400, {"error": "Missing uri"})
            return
        norm = os.path.abspath(uri.removeprefix("file:///").replace("/", os.sep))
        allowed_roots = [os.path.abspath(ANTIGRAVITY_DATA_DIR), os.path.abspath(UPLOAD_DIR)]
        if not any(norm.lower().startswith(root.lower() + os.sep) or norm.lower() == root.lower() for root in allowed_roots):
            self._send_json(403, {"error": "Forbidden path"})
            return
        if not os.path.isfile(norm):
            self._send_json(404, {"error": "File not found"})
            return
        mime = mimetypes.guess_type(norm)[0] or "application/octet-stream"
        size = os.path.getsize(norm)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(norm, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _handle_upload(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"error": "空文件"})
            return
        if length > MAX_UPLOAD_BYTES:
            self._send_json(413, {"error": "文件超过 25MB 限制"})
            return
        raw_name = urllib.parse.unquote(self.headers.get("X-File-Name") or "attachment.bin")
        base_name = os.path.basename(raw_name.replace("\\", "/")) or "attachment.bin"
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base_name).strip() or "attachment.bin"

        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not content_type or content_type == "application/octet-stream":
            guessed = mimetypes.guess_type(safe_name)[0]
            if guessed:
                content_type = guessed
            else:
                ext = os.path.splitext(safe_name)[1].lower()
                ext_map = {
                    ".m4a": "audio/mp4",
                    ".aac": "audio/aac",
                    ".mp3": "audio/mpeg",
                    ".wav": "audio/wav",
                    ".ogg": "audio/ogg",
                    ".opus": "audio/opus",
                    ".flac": "audio/flac",
                    ".amr": "audio/amr",
                    ".mp4": "video/mp4",
                    ".webm": "video/webm",
                    ".mov": "video/mp4",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".gif": "image/gif",
                }
                content_type = ext_map.get(ext, "application/octet-stream")

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        uid = uuid.uuid4().hex[:10]
        dest_path = os.path.join(UPLOAD_DIR, f"{uid}_{safe_name}")
        remaining = length
        with open(dest_path, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)

        self._send_json(200, {
            "uploadId": uid,
            "uri": dest_path.replace("\\", "/"),
            "displayName": safe_name,
            "description": safe_name,
            "mimeType": content_type,
            "size": os.path.getsize(dest_path),
        })

    def do_GET(self) -> None:
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/api/status"):
                self._send_json(200, self.service.get_status(self.bridge_port))
            elif path == "/api/models":
                self._send_json(200, self.service.get_models())
            elif path == "/api/workspaces":
                self._send_json(200, self.service.get_workspaces())
            elif path == "/api/media":
                self._handle_media_get((qs.get("uri") or [""])[0])
            elif path == "/api/conversations":
                q = (qs.get("q") or [""])[0]
                include_archive = (qs.get("includeArchive") or ["true"])[0].lower() != "false"
                items = self.service.list_conversations(query=q, include_archive=include_archive)
                self._send_json(200, {"conversations": items, "count": len(items)})
            elif path.startswith("/api/conversations/") and path.endswith("/stream"):
                cid = path[len("/api/conversations/"):-len("/stream")].strip("/")
                expect_reply = (qs.get("afterSend") or ["0"])[0].lower() in ("1", "true")
                self._handle_sse(cid, expect_reply=expect_reply)
            elif path.startswith("/api/conversations/"):
                cid = path[len("/api/conversations/"):].strip("/")
                max_steps = int((qs.get("maxSteps") or ["400"])[0])
                self._send_json(200, self.service.get_conversation(cid, max_steps=max_steps))
            else:
                self._send_json(404, {"error": f"Unknown route: {path}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self) -> None:
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if path == "/api/upload":
                self._handle_upload()
                return
            body = self._read_json()
            if path == "/api/launch":
                hub = self.service.hub.launch_antigravity()
                self._send_json(200, {"ok": True, "hubPort": hub.get("httpPort"), "hubPid": hub.get("pid")})
            elif path == "/api/conversations/new":
                msg = str(body.get("message") or "").strip()
                media = body.get("media") if isinstance(body.get("media"), list) else []
                if not msg and not media:
                    self._send_json(400, {"error": "消息内容和附件不能同时为空"})
                    return
                res = self.service.start_and_send(
                    message=msg,
                    model_id=body.get("model"),
                    workspace_uri=body.get("workspaceUri"),
                    auto_exec_policy=body.get("autoExecutionPolicy"),
                    media=media,
                )
                self._send_json(200, res)
            elif path.startswith("/api/conversations/") and path.endswith("/send"):
                cid = path[len("/api/conversations/"):-len("/send")].strip("/")
                msg = str(body.get("message") or "").strip()
                media = body.get("media") if isinstance(body.get("media"), list) else []
                if not msg and not media:
                    self._send_json(400, {"error": "消息内容和附件不能同时为空"})
                    return
                res = self.service.send_message(
                    cascade_id=cid,
                    message=msg,
                    model_id=body.get("model"),
                    auto_exec_policy=body.get("autoExecutionPolicy"),
                    media=media,
                )
                self._send_json(200, res)
            elif path.startswith("/api/conversations/") and path.endswith("/cancel"):
                cid = path[len("/api/conversations/"):-len("/cancel")].strip("/")
                self._send_json(200, self.service.cancel(cid))
            elif path.startswith("/api/conversations/") and path.endswith("/interact"):
                cid = path[len("/api/conversations/"):-len("/interact")].strip("/")
                self._send_json(200, self.service.interact(cid, body))
            else:
                self._send_json(404, {"error": f"Unknown route: {path}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_DELETE(self) -> None:
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if path.startswith("/api/conversations/"):
                cid = path[len("/api/conversations/"):].strip("/")
                self._send_json(200, self.service.delete(cid))
            else:
                self._send_json(404, {"error": f"Unknown route: {path}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    @staticmethod
    def _has_terminal_reply_after_last_user(data: dict[str, Any]) -> bool:
        msgs = data.get("messages") or []
        last_user_idx = -1
        for i, m in enumerate(msgs):
            if m.get("role") == "user":
                last_user_idx = i
        if last_user_idx < 0:
            return False
        for m in msgs[last_user_idx + 1:]:
            if m.get("role") == "assistant" and (m.get("text") or "").strip():
                if (m.get("status") or "DONE") in ("DONE", ""):
                    return True
            if m.get("role") == "error":
                return True
        return False

    def _handle_sse(self, cascade_id: str, expect_reply: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        last_digest = ""
        idle_ticks = 0
        saw_running = False
        started_at = time.monotonic()
        try:
            while True:
                data = self.service.get_conversation(cascade_id)
                encoded = json.dumps(data, ensure_ascii=False)
                digest = hashlib.md5(encoded.encode("utf-8")).hexdigest()
                if digest != last_digest:
                    last_digest = digest
                    self.wfile.write(f"event: state\ndata: {encoded}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()

                if data.get("status") != "IDLE":
                    saw_running = True
                    idle_ticks = 0
                    time.sleep(0.35)
                else:
                    if (
                        expect_reply
                        and not saw_running
                        and not self._has_terminal_reply_after_last_user(data)
                        and (time.monotonic() - started_at) < 15.0
                    ):
                        idle_ticks = 0
                        time.sleep(0.5)
                        continue

                    idle_ticks += 1
                    needed_idle = 4 if (saw_running or expect_reply) else 3
                    if idle_ticks >= needed_idle:
                        # Final confirmation fetch before closing so no settled reply is missed.
                        final_data = self.service.get_conversation(cascade_id)
                        final_encoded = json.dumps(final_data, ensure_ascii=False)
                        final_digest = hashlib.md5(final_encoded.encode("utf-8")).hexdigest()
                        if final_digest != last_digest:
                            last_digest = final_digest
                            self.wfile.write(f"event: state\ndata: {final_encoded}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        if final_data.get("status") != "IDLE":
                            saw_running = True
                            idle_ticks = 0
                            time.sleep(0.35)
                            continue
                        self.wfile.write(b"event: done\ndata: {}\n\n")
                        self.wfile.flush()
                        break
                    time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            try:
                err_json = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.wfile.write(f"event: error\ndata: {err_json}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass


def main() -> None:
    if sys.stdout is None or sys.stderr is None:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")
        log_f = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_f
        if sys.stderr is None:
            sys.stderr = log_f

    parser = argparse.ArgumentParser(description="Antigravity Mobile PC Bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ANTIGRAVITY_BRIDGE_PORT", DEFAULT_PORT)))
    parser.add_argument("--token", default=os.environ.get("ANTIGRAVITY_BRIDGE_TOKEN", ""))
    args = parser.parse_args()

    hub = AntigravityHubClient()
    service = AntigravityBridgeService(hub)

    class ConfiguredHandler(BridgeHandler):
        pass

    ConfiguredHandler.service = service
    ConfiguredHandler.bridge_port = args.port
    ConfiguredHandler.auth_token = args.token

    class QuietHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

        def handle_error(self, request: Any, client_address: Any) -> None:
            exc_type = sys.exc_info()[0]
            if exc_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            super().handle_error(request, client_address)

    server = QuietHTTPServer((args.host, args.port), ConfiguredHandler)
    ips = get_lan_ips()
    print(f"Antigravity Mobile Bridge v{BRIDGE_VERSION} listening on http://{args.host}:{args.port}")
    for ip in ips:
        print(f"  LAN URL: http://{ip}:{args.port}")
    hub_info = hub.discover_hub()
    if hub_info:
        print(f"  Linked to Antigravity.exe language_server PID={hub_info['pid']} HTTP={hub_info['httpPort']}")
    else:
        print("  Waiting for Antigravity.exe to start...")
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
