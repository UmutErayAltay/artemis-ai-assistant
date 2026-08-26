"""`plugins/assistant_plugin.py` testleri.

`assistant.reply` bu projedeki en KRİTİK zararsız tool: modelin "hiçbir
şey yapma" seçeneği. Şema `minItems: 1` ile bir tool çağrısı ZORUNLU
kıldığı için, bu tool olmadan model anlamadığı her girdide rastgele bir
tool seçiyordu ("Sen kimsin?" -> masaüstünde dosya oluşturuldu).

Buna rağmen hiç doğrudan testi yoktu: yalnızca registry'de adı geçiyor
mu diye bakılıyordu. Özellikle boş-mesaj geri düşüşü hiç
çalıştırılmamıştı.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.enums import DangerLevel
from core.plugin_loader import TOOL_REGISTRY


def test_reply_returns_the_message_and_changes_nothing(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Tek işi konuşmak: dosya sisteminde HİÇBİR iz bırakmamalı."""

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    oncesi = set(settings.desktop_path.iterdir())

    result = dispatcher.dispatch(
        {"tool": "assistant.reply", "arguments": {"message": "Ben Artemis, yerel asistanınım."}}
    )

    assert result.success is True
    assert result.message == "Ben Artemis, yerel asistanınım."
    assert result.data == {"reply": "Ben Artemis, yerel asistanınım."}
    assert set(settings.desktop_path.iterdir()) == oncesi


@pytest.mark.parametrize("bos_mesaj", ["", "   ", "\n", "\t  \n"])
def test_empty_message_falls_back_to_an_honest_answer(
    dispatcher: ToolDispatcher, bos_mesaj: str
) -> None:
    """Model boş bir cevap ürettiyse SESSİZ kalmak yanlış olur.

    Kullanıcı en azından duyulduğunu bilmeli. Bu geri düşüş kodda
    yazılıydı ama hiç çalıştırılmamıştı.
    """

    result = dispatcher.dispatch({"tool": "assistant.reply", "arguments": {"message": bos_mesaj}})

    assert result.success is True
    assert result.message == "Bunu anlayamadım."


def test_message_is_a_required_argument(dispatcher: ToolDispatcher) -> None:
    """Eksik zorunlu argüman, tool çalışmadan ÖNCE yakalanmalı."""

    result = dispatcher.dispatch({"tool": "assistant.reply", "arguments": {}})

    assert result.success is False
    assert "message" in result.message


def test_reply_is_safe_and_never_asks_for_confirmation() -> None:
    """Hiçbir yan etkisi olmayan bir tool onay istememeli.

    İstese, modelin "emin değilim" çıkışı kullanıcıya sürtünme yaratır ve
    rastgele bir tool seçmek yeniden cazip hâle gelirdi.
    """

    assert TOOL_REGISTRY["assistant.reply"].danger_level is DangerLevel.SAFE


def test_reply_never_requires_confirmation_through_the_dispatcher(
    dispatcher: ToolDispatcher,
) -> None:
    result = dispatcher.dispatch({"tool": "assistant.reply", "arguments": {"message": "merhaba"}})

    assert result.requires_confirmation is False


def test_non_string_message_does_not_crash(dispatcher: ToolDispatcher) -> None:
    """Şema `string` diyor ama savunma katmanı yine de çökmemeli."""

    result = dispatcher.dispatch({"tool": "assistant.reply", "arguments": {"message": 42}})

    # Şema doğrulaması bunu zaten reddeder; önemli olan ÇÖKMEMESİ.
    assert result.success is False
    assert "message" in result.message
