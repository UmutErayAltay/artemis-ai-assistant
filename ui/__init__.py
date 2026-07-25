"""Artemis'in görsel katmanı (PyQt6).

Bu paket yalnızca GÖRÜNTÜ ve kullanıcı etkileşiminden sorumludur; ses
tanıma, LLM veya tool çalıştırma mantığı burada YAŞAMAZ. Arayüz, dışarıdan
çağrılan basit metotlarla (`show_listening()`, `set_amplitude()`, ...)
sürülür — böylece ileride arayüz tamamen değiştirilse bile `core/` ve
`voice/` katmanlarında hiçbir değişiklik gerekmez.
"""
