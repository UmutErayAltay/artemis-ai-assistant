"""`projects/interview.py` testleri: çok turlu proje görüşmesi ve sözleşme uyumu.

LLM sahte (sıralı cevaplar döndürür); depo GERÇEK SQLite (`tmp_path`).
Sözleşme testleri README §16a sınıfı hatayı yakalar: şema, prompt ve
`ProjectSpec` aynı alanları konuşmazsa model ya istenen biçimi
üretemez ya da ürettiği spec doğrulamadan geçemez.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.settings import ProjelerSettings
from core.llm_client import LLMResponseParseError
from core.prompt_builder import strip_developer_header
from models.project_models import ProjectSpec
from projects.interview import (
    PROMPT_PATH,
    RESPONSE_SCHEMA,
    SPEC_FIELDS,
    ProjectInterview,
    first_question,
)
from projects.store import INTERVIEW_ACTIVE, INTERVIEW_CANCELLED, INTERVIEW_READY, ProjectStore

_VALID_SPEC: dict[str, Any] = {
    "ad": "Not Defteri",
    "slug": "not-defteri",
    "amac": "Hızlı not almak.",
    "kullanici": "Umut",
    "ozellikler": ["not ekle", "notları listele"],
    "teknoloji": "Python + SQLite, CLI",
    "m1_bitti_olcutu": "pytest yeşil ve `python main.py liste` notları gösteriyor",
    "kodlayici": "ucretsiz",
    "notlar": "",
}

_EMPTY_SPEC: dict[str, Any] = {field: "" for field in SPEC_FIELDS} | {"ozellikler": [], "kodlayici": "claude"}


class ScriptedLLM:
    """`get_structured_response` çağrılarına sırayla cevap veren sahte istemci."""

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.inputs: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    def get_structured_response(
        self, system_prompt: str, user_input: str, schema: dict[str, Any], schema_name: str = "cevap"
    ) -> dict[str, Any]:
        self.inputs.append(user_input)
        self.schemas.append(schema)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def projeler(tmp_path: Path) -> ProjelerSettings:
    return ProjelerSettings(root=tmp_path / "Projeler", state_dir=tmp_path / "durum", interview_max_questions=4)


@pytest.fixture
def store(projeler: ProjelerSettings) -> ProjectStore:
    return ProjectStore(projeler.state_dir / "projeler.db")


def _open(store: ProjectStore) -> Any:
    store.create_interview("bir not uygulaması", first_question())
    interview = store.open_interview()
    assert interview is not None
    return interview


# --- sözleşme --------------------------------------------------------------


def test_spec_fields_match_the_pydantic_model() -> None:
    assert set(SPEC_FIELDS) == set(ProjectSpec.model_fields)


def test_response_schema_is_strict_mode_compatible() -> None:
    """OpenRouter strict modu: her nesnede TÜM alanlar required, ek alan yok."""

    def check(node: dict[str, Any]) -> None:
        if node.get("type") == "object":
            assert set(node["required"]) == set(node["properties"]), node
            assert node["additionalProperties"] is False
            for child in node["properties"].values():
                check(child)

    check(RESPONSE_SCHEMA)


def test_prompt_describes_every_schema_field() -> None:
    body = strip_developer_header(PROMPT_PATH.read_text(encoding="utf-8"))
    for field in (*SPEC_FIELDS, "durum", "mesaj", "spec", "soru", "hazir"):
        assert f'"{field}"' in body, f"prompt '{field}' alanını anlatmıyor"
    assert "{max_soru}" in body and "{kalan_soru}" in body


# --- akış ------------------------------------------------------------------


def test_question_then_ready_then_start_produces_a_tool_call(store: ProjectStore, projeler: ProjelerSettings) -> None:
    llm = ScriptedLLM(
        [
            {"durum": "soru", "mesaj": "CLI mı web mi?", "spec": _EMPTY_SPEC},
            {"durum": "hazir", "mesaj": "Tamamdır.", "spec": _VALID_SPEC},
        ]
    )
    interview = ProjectInterview(store, llm, projeler)

    turn = interview.handle(_open(store), "Kendim için, notlarımı kaybediyorum")
    assert turn.message == "CLI mı web mi?"
    assert turn.tool_call is None
    assert llm.schemas[0] is RESPONSE_SCHEMA
    assert "Umut: Kendim için" in llm.inputs[0]

    turn = interview.handle(store.open_interview(), "CLI yeter")
    current = store.open_interview()
    assert current.status == INTERVIEW_READY
    assert current.spec["slug"] == "not-defteri"
    assert "Spec hazır" in turn.message and "başlat" in turn.message
    assert "ücretsiz model" in turn.message

    turn = interview.handle(current, "başlat")
    assert turn.tool_call == {
        "tool": "proje.islem",
        "arguments": {"islem": "baslat", "ad": "not-defteri", "kodlayici": "ucretsiz"},
    }
    assert len(llm.inputs) == 2, "'başlat' modele gitmemeli"


def test_long_sentence_with_start_word_is_a_revision_not_a_start(
    store: ProjectStore, projeler: ProjelerSettings
) -> None:
    revised = _VALID_SPEC | {"teknoloji": "Go"}
    llm = ScriptedLLM([{"durum": "hazir", "mesaj": "Go yaptım.", "spec": revised}])
    interview = _open(store)
    interview.status, interview.spec = INTERVIEW_READY, dict(_VALID_SPEC)
    store.save_interview(interview)

    turn = ProjectInterview(store, llm, projeler).handle(interview, "başlat ama önce teknolojiyi Go yap lütfen")

    assert turn.tool_call is None
    assert store.open_interview().spec["teknoloji"] == "Go"
    assert "MEVCUT SPEC" in llm.inputs[0]


@pytest.mark.parametrize("cevap", ["vazgeç", "iptal et", "boş ver bunu"])
def test_cancel_closes_the_interview_without_a_model_call(
    store: ProjectStore, projeler: ProjelerSettings, cevap: str
) -> None:
    llm = ScriptedLLM([])
    turn = ProjectInterview(store, llm, projeler).handle(_open(store), cevap)

    assert "kapattım" in turn.message
    assert store.open_interview() is None
    assert llm.inputs == []


def test_model_failure_keeps_the_transcript_unchanged(store: ProjectStore, projeler: ProjelerSettings) -> None:
    """Hata turunda kullanıcının cevabı kaydedilmez — tekrar yazınca çift girmesin."""

    before = _open(store)
    llm = ScriptedLLM([ConnectionError("model yok"), LLMResponseParseError("bozuk")])
    interview = ProjectInterview(store, llm, projeler)

    for _ in range(2):
        turn = interview.handle(store.open_interview(), "web olsun")
        assert "tekrar" in turn.message
    assert store.open_interview().transcript == before.transcript


def test_question_limit_forces_a_spec(store: ProjectStore, projeler: ProjelerSettings) -> None:
    """Hak bitince model hâlâ soru sorsa da elindeki (her turda zorunlu) taslak kabul edilir."""

    limited = projeler.model_copy(update={"interview_max_questions": 1})
    llm = ScriptedLLM([{"durum": "soru", "mesaj": "Bir şey daha?", "spec": _VALID_SPEC}])

    ProjectInterview(store, llm, limited).handle(_open(store), "notlar için")

    assert "SORU HAKKIN BİTTİ" in llm.inputs[0]
    assert store.open_interview().status == INTERVIEW_READY


def test_invalid_ready_spec_stays_active_and_names_missing_fields(
    store: ProjectStore, projeler: ProjelerSettings
) -> None:
    broken = _VALID_SPEC | {"ozellikler": [], "teknoloji": ""}
    llm = ScriptedLLM([{"durum": "hazir", "mesaj": "Bitti.", "spec": broken}])

    turn = ProjectInterview(store, llm, projeler).handle(_open(store), "yeter")

    assert store.open_interview().status == INTERVIEW_ACTIVE
    assert "ozellikler" in turn.message and "teknoloji" in turn.message


def test_cancelled_interview_is_not_reopened(store: ProjectStore, projeler: ProjelerSettings) -> None:
    interview = _open(store)
    store.mark_interview(interview.id, INTERVIEW_CANCELLED)
    assert store.open_interview() is None
