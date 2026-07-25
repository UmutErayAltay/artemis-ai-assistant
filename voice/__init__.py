"""Artemis'in ses katmanı: mikrofon, uyandırma sözcüğü, STT ve TTS.

Katmanlar bilinçli olarak birbirinden bağımsızdır ve hiçbiri arayüzü
(`ui/`) veya tool çalıştırmayı (`core/`) tanımaz:

    audio.py               — ortak mikrofon akışı ve ses seviyesi ölçümü (temel)
    wake_word.py           — enerji kapısı + Whisper "tiny"; sessiz odada Whisper hiç çağrılmaz
    stt.py / stt_cloud.py  — komut tanıma: yerel Whisper / Groq (bulut)
    tts.py / tts_cloud.py  — sesli cevap: yerel Piper / Microsoft Edge (bulut)
    router.py              — hangi sağlayıcının (bulut/yerel) kullanılacağına karar verir

Hepsini birbirine bağlayan orkestrasyon `core/voice_loop.py`'dadır; STT/TTS
çağrıları oraya doğrudan `stt.py`/`tts.py` üzerinden değil, `router.py`'nin
`SpeechToTextRouter`/`TextToSpeechRouter` sınıfları üzerinden yapılır.

TASARIM NOTU — neden hibrit (bulut + yerel)?
    İnternet varken bulut sağlayıcılar (Groq STT, Microsoft Edge TTS) yabancı
    özel isimlerde (Discord, Riot Client gibi) yerel modellerden belirgin
    biçimde daha doğrudur. Ama Artemis offline-first bir asistan: internet
    yokken ya da bulut hata verdiğinde de çalışmaya devam etmesi gerekir. Bu
    yüzden `router.py` önce buluta dener, herhangi bir hatada sessizce yerele
    düşer ve bir süre (soğuma) buluta tekrar denemez — aksi halde internet
    kapalıyken her komut önce zaman aşımını bekleyip asistanı kullanılamaz hale
    getirirdi (bkz. `router.py` modül dokümantasyonu).

TASARIM NOTU — neden uyandırma ve komut tanıma AYRI modeller kullanır?
    Uyandırma sözcüğü SÜREKLİ dinlenir; bu yüzden çok hafif olmalı. Whisper bu
    yüzden SÜREKLİ çalıştırılmaz: önce ucuz bir enerji ölçümüyle konuşma olup
    olmadığına bakılır, Whisper yalnızca gerçekten konuşma algılandığında ve
    yalnızca en küçük ("tiny") boyutuyla çalıştırılır (bkz. `wake_word.py`).
    Gerçek komut ise yalnızca uyandıktan sonra, kısa bir süre için tanınır;
    orada doğruluk kritiktir (yanlış anlaşılan bir komut yanlış tool çağrısı
    demektir), bu yüzden daha büyük bir model (yerelde `whisper_model_size`,
    varsayılan "small") ya da bulut kullanılır. Bu, projenin
    `ollama_keep_alive` felsefesinin ses tarafındaki karşılığıdır: pahalı işi
    yalnızca gerektiğinde çalıştır.
"""
