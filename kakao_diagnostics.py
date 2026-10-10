"""Read-only local readiness checks for the operator's connection panel.

This module does not initialize a bridge, decrypt credentials, contact a
server, or control desktop windows. The sole subprocess is ``node --version``
with a short timeout. Run diagnose_local in a worker, not on the Tk thread.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from kakao_direct import DirectKakaoConfig, EXPECTED_SDK_VERSION
from kakao_tablet_reauth import default_account_vault_path


@dataclass(frozen=True)
class DiagnosticItem:
    label: str
    state: str  # ok, warning, error
    detail: str


def _node_item(config: DirectKakaoConfig) -> DiagnosticItem:
    try:
        node = Path(config.node_path)
        if not node.is_file():
            found = shutil.which(str(node))
            if not found:
                return DiagnosticItem("Node.js", "error", "Node.js 실행 파일이 없습니다. 설치 경로 또는 KAKAO_NODE_PATH 설정을 확인하세요.")
            node = Path(found)
        result = subprocess.run(
            [str(node), "--version"], capture_output=True, text=True,
            timeout=3, check=False, shell=False,
            env={key: value for key, value in os.environ.items() if key.upper() != "NODE_OPTIONS"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        version = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
            return DiagnosticItem("Node.js", "error", "Node.js 버전을 확인하지 못했습니다. 실행 파일 경로를 확인하세요.")
        return DiagnosticItem("Node.js", "ok", f"{version} · {node}")
    except subprocess.TimeoutExpired:
        return DiagnosticItem("Node.js", "error", "버전 확인이 3초 안에 끝나지 않았습니다. Node.js 실행 환경을 확인하세요.")
    except Exception:
        return DiagnosticItem("Node.js", "error", "Node.js 실행 파일을 실행하지 못했습니다. 경로와 실행 권한을 확인하세요.")


def _sdk_package(root: Path) -> Path | None:
    if root.is_dir():
        package = root / "package.json"
        return package if package.is_file() else None
    if root.is_file() and root.suffix.lower() in {".js", ".mjs", ".cjs"}:
        # Config accepts a package entry file, including dist/index.js.
        # Bound traversal; do not crawl unrelated parent directories.
        for directory in list(root.parents)[:3]:
            package = directory / "package.json"
            if package.is_file():
                try:
                    if json.loads(package.read_text(encoding="utf-8")).get("name") == "agent-messenger":
                        return package
                except (OSError, ValueError, AttributeError):
                    return package
    return None


def _sdk_item(config: DirectKakaoConfig) -> DiagnosticItem:
    try:
        configured = Path(config.sdk_root)
        package = _sdk_package(configured)
        if package is None:
            return DiagnosticItem("직접 전송 SDK", "error", f"agent-messenger 설치를 찾지 못했습니다. bridge 폴더에서 고정 의존성을 설치하거나 SDK 경로를 확인하세요. · {configured}")
        if package.stat().st_size > 1024 * 1024:
            return DiagnosticItem("직접 전송 SDK", "error", "SDK package.json 크기가 비정상입니다. 설치를 확인하세요.")
        payload = json.loads(package.read_text(encoding="utf-8"))
        if payload.get("name") != "agent-messenger":
            return DiagnosticItem("직접 전송 SDK", "error", "설정된 SDK 경로가 agent-messenger 패키지를 가리키지 않습니다.")
        version = payload.get("version")
        if version != EXPECTED_SDK_VERSION:
            return DiagnosticItem("직접 전송 SDK", "error", f"검증된 SDK 버전 {EXPECTED_SDK_VERSION}과 설치 버전이 다릅니다. bridge 폴더의 고정 의존성을 다시 설치하세요. · {package}")
        project_root = Path(__file__).resolve().parent
        location = "프로젝트 내부" if package.resolve().is_relative_to(project_root) else "외부 경로"
        return DiagnosticItem("직접 전송 SDK", "ok", f"agent-messenger {version} · {location} · {package.parent}")
    except Exception:
        return DiagnosticItem("직접 전송 SDK", "error", "SDK 설치 정보를 읽지 못했습니다. package.json과 SDK 경로를 확인하세요.")


def _coordinates_item(pc_config: Mapping[str, Any]) -> DiagnosticItem:
    try:
        coordinates = pc_config.get("search_box") or {}
        raw_x, raw_y = coordinates.get("x"), coordinates.get("y")
        if isinstance(raw_x, bool) or isinstance(raw_y, bool):
            raise ValueError("boolean coordinate")
        x, y = float(raw_x), float(raw_y)
        if not all(math.isfinite(value) and value > 0 for value in (x, y)):
            raise ValueError("invalid coordinates")
        return DiagnosticItem("PC 검색창 좌표", "ok", f"설정됨: X {x:g}, Y {y:g}. 실제 카카오톡 창 위치는 이 점검에서 조작하거나 확인하지 않습니다.")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return DiagnosticItem("PC 검색창 좌표", "warning", "예약·첨부·2차 화면 전송 전에 [화면 좌표 설정]에서 검색창 위치를 저장하세요.")


def diagnose_local(config: DirectKakaoConfig, pc_config: Mapping[str, Any]) -> tuple[DiagnosticItem, ...]:
    """Return five local checks. Existence of a vault is not authentication."""
    node = _node_item(config)
    try:
        bridge = Path(config.bridge_script)
        bridge_item = DiagnosticItem("직접 전송 브리지", "ok", str(bridge)) if bridge.is_file() else DiagnosticItem(
            "직접 전송 브리지", "error", "브리지 파일이 없습니다. 프로젝트 파일과 bridge_script 설정을 확인하세요.",
        )
    except (OSError, TypeError, ValueError):
        bridge_item = DiagnosticItem("직접 전송 브리지", "error", "브리지 파일 경로를 확인하지 못했습니다.")
    try:
        vault_exists = Path(config.credential_vault).is_file()
        vault = DiagnosticItem(
            "직접 전송 인증 파일", "ok" if vault_exists else "error",
            "저장 파일 있음. 파일 존재만 확인했으며 인증 유효성은 연결 테스트에서 확인하세요." if vault_exists else
            "저장 파일 없음. Spoiler의 태블릿 설정에서 카카오 계정을 다시 등록한 뒤 연결 테스트를 실행하세요.",
        )
    except (OSError, TypeError, ValueError):
        vault = DiagnosticItem("직접 전송 인증 파일", "error", "인증 파일 존재 여부를 확인하지 못했습니다. 저장 경로와 접근 권한을 확인하세요.")
    try:
        account_detail = (
            " 자동 복구 계정도 Windows 사용자 암호화 저장소에 준비되어 있습니다."
            if default_account_vault_path().is_file()
            else " 자동 복구 계정은 아직 준비되지 않았습니다."
        )
    except OSError:
        account_detail = " 자동 복구 계정 준비 여부는 확인하지 못했습니다."
    vault = DiagnosticItem(vault.label, vault.state, vault.detail + account_detail)
    return node, bridge_item, _sdk_item(config), vault, _coordinates_item(pc_config)


def connection_guidance(code: str | None) -> tuple[str, ...]:
    """Explain a known failure code without echoing error text or resetting data."""
    if code == "get_chats_failed":
        return (
            "로그인이나 Spoiler 연결이 아니라 카카오 전체 채팅 목록 페이지 조회가 실패했습니다.",
            "Hub는 저장된 매칭을 chat ID별 CHATINFO로 다시 검증합니다.",
            "미일치 대상만 [수동 매칭]에서 확인하세요.",
        )
    if code in {"refresh_rejected", "authentication_failed", "auth_failed", "login_failed", "credentials_missing", "credentials_invalid"}:
        first = "저장된 인증 정보가 없습니다." if code == "credentials_missing" else "카카오 인증을 갱신하지 못했거나 저장된 인증 정보가 유효하지 않습니다."
        return (
            first,
            "자동 복구 계정이 준비되어 있으면 Hub가 Spoiler 태블릿 설정을 한 번 다시 등록합니다.",
            "휴대폰 인증번호가 표시되면 카카오톡에서 확인한 뒤 Hub의 [직접 연결 확인]을 다시 실행하세요.",
        )
    if code in {"timeout", "request_timeout", "ready_timeout"}:
        return (
            "연결 응답이 제한 시간 안에 도착하지 않았습니다.",
            "인터넷 연결과 카카오톡·Spoiler 실행 상태를 확인한 뒤 연결 테스트를 다시 실행하세요.",
            "계속 인증 오류가 나타나면 Spoiler의 태블릿 설정에서 카카오 계정을 다시 등록하세요.",
        )
    if code in {"process_disconnected", "disconnected", "bridge_closed", "bridge_start_failed"}:
        return (
            "직접 전송 연결 또는 브리지 프로세스가 종료되었습니다.",
            "로컬 진단의 Node.js·브리지·SDK 항목을 먼저 확인하고 Hub 연결 테스트를 다시 실행하세요.",
            "인증 갱신 오류가 함께 나타나면 Spoiler의 태블릿 설정에서 카카오 계정을 다시 등록하세요.",
        )
    if code == "send_outcome_unknown":
        return (
            "직접 전송 성공 여부를 확정할 수 없습니다.",
            "카카오톡 대화방에서 실제 도착 여부를 먼저 확인하세요. 이 항목은 기본 재전송 대상에서 제외됩니다.",
        )
    if code in {"sdk_version_mismatch", "sdk_missing", "node_missing"}:
        return (
            "직접 전송 실행 환경을 확인해야 합니다.",
            f"로컬 진단에서 Node.js와 agent-messenger {EXPECTED_SDK_VERSION} 설치 경로를 확인하세요.",
        )
    return (
        "직접 전송 연결을 확인하지 못했습니다.",
        "로컬 진단 결과를 확인한 뒤 연결 테스트를 다시 실행하세요. 인증 정보나 원본 오류 내용을 공유하지 않아도 됩니다.",
    )
