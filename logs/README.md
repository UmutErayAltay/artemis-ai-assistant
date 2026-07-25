Bu klasör boş bırakılmıştır; `utils/logger.py::setup_logging()` her
çalıştırmada bu dizini (yoksa) otomatik olarak oluşturur ve
`artemis.log` dosyasını buraya yazar (RotatingFileHandler ile, 3 yedek,
her biri en fazla ~1MB).
