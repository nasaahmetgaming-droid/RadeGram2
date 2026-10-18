# RadeGram — Kalıcı veritabanı ve medya deposu kurulumu

Bu sürüm, yerelde çalışırken SQLite ve `uploads/` klasörünü kullanmaya devam eder. Render ortamında kalıcı veri için PostgreSQL, kalıcı fotoğraf/video/ses dosyaları için Cloudinary kullanacak şekilde hazırlanmıştır.

## Önce önemli not

- Render ücretsiz web hizmetinin yerel dosya sistemi geçicidir. SQLite dosyasını veya `uploads/` klasörünü Render'da kalıcı veri olarak kullanmayın.
- Bu sürüm Render'da kalıcı PostgreSQL için `DATABASE_URL`, medya için Cloudinary değişkenleri gerektirir. Bunlar girilmezse yeni hesaplar veritabanına yazılamaz (veritabanı yoksa servis başlangıcı hata verir); Cloudinary ayarlanmadan Render üzerinde medya yükleme kalıcıymış gibi yerel diske kaydedilmez.
- Bu ZIP mevcut Render SQLite veritabanını otomatik olarak taşımıyor. Eski hesaplar/gönderiler önemliyse eski veritabanının ayrıca dışa aktarılıp dönüştürülmesi gerekir. ZIP'e hiçbir veritabanı veya kullanıcı yüklemesi eklenmemiştir.

## 1. Kalıcı PostgreSQL oluştur

Neon gibi PostgreSQL sağlayıcısında bir veritabanı oluştur. Bağlantı adresini (connection string) al; gizli tut. Ücretsiz planların depolama, bağlantı ve trafik sınırları olabilir.

## 2. Kalıcı medya deposu oluştur

Cloudinary hesabı oluştur ve Dashboard'dan `Cloud name`, `API Key` ve `API Secret` değerlerini al. Bu sürüm görsel, video, hikâye, profil görseli ve gönderi ses dosyalarını Cloudinary'ye yükler; URL'lerini veritabanına kaydeder. Ücretsiz planın kullanım kotaları vardır. API Secret'ı GitHub'a veya istemci uygulamasına koyma.

## 3. Render ortam değişkenleri

Render Dashboard → RadeGram servisi → **Environment** bölümüne şu değişkenleri ekle:

- `DATABASE_URL` = PostgreSQL bağlantı adresi (SSL gerektiren bağlantı adresini kullan)
- `CLOUDINARY_CLOUD_NAME` = Cloudinary Cloud Name
- `CLOUDINARY_API_KEY` = Cloudinary API Key
- `CLOUDINARY_API_SECRET` = Cloudinary API Secret

Kaydet ve yeni deploy başlat. Bu repo Blueprint olarak kullanılıyorsa `render.yaml` bu dört değeri senin girmen için `sync: false` olarak tanımlar.

## 4. Kontrol et

- `https://SENIN-RADEGRAM-ADRESIN.onrender.com/api/health` adresini aç; `ok: true` yanıtını doğrula.
- Yeni test hesabı oluştur, gönderi ve profil fotoğrafı yükle.
- Deploy yaptıktan sonra tekrar giriş yap; veritabanı hesabı korumalı ve medya URL'leri hâlâ açılıyor olmalı.

## Güvenlik ve sınırlar

- Ücretsiz servisler kalıcı veri depolama garantisi veya sınırsız kullanım sağlamaz; plan kotalarını ve sağlayıcı şartlarını takip et. Önemli veriler için yedekleme ayarla.
- Render ücretsiz hizmeti yine boşta kaldığında uyuyabilir. Kalıcı depolama, servisin 7/24 uyanık kalacağı anlamına gelmez.
- GitHub'a `.env`, veritabanı, `uploads/` veya API Secret yükleme.
- Bu değişiklik mevcut web API'sini korur; mobil uygulamanın arayüzünü kendi başına üretmez.
