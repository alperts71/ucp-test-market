# UCP Test Market

Yapay zekâ alışveriş asistanlarının (ajanların) **UCP** ile alışveriş yapabildiği küçük bir **test mağazası**.
Gerçek ürün satılmaz, gerçek para çekilmez.

> **UCP nedir?** "Universal Commerce Protocol". Bir yapay zekâ ajanının bir mağazayla
> konuşması için ortak kurallar. Ajan önce mağazanın "tanıtım kartını" okur
> (`/.well-known/ucp`), sonra ürün arar, sepet/ödeme oturumu açar ve ödemeyi gönderir.
> Bu mağaza UCP'nin güncel sürümü **2026-08-25**'i izler.

## İçinde ne var?

| Parça | Ne işe yarar |
| --- | --- |
| `app/` | Mağazanın kendisi (Python). |
| `app/products.json` | 20 elektronik ürün, fiyatlar 10–1000 dolar arası rastgele. "Action Camera" bilerek **stokta yok** (stok bitti senaryosunu denemek için). |
| `client/ucp_test_client.py` | Bağımsız test istemcisi: mağazayla bir ajan gibi konuşur, tüm senaryoları dener. |
| `tests/` | Otomatik testler. Cevapları resmi UCP Python kütüphanesiyle (ucp-sdk) kontrol eder. |
| `Dockerfile` | Google Cloud Run'a yüklemek için paket tarifi (henüz yüklenmedi). |

### Mağaza neleri destekliyor

- **Tanıtım kartı** (`GET /.well-known/ucp`): mağazanın neler yapabildiğini anlatır.
- **Ürün arama** (`POST /catalog/search`) ve **ürün bulma** (`POST /catalog/lookup`, `POST /catalog/product`).
- **Ödeme oturumu** (checkout): oluştur, güncelle, tamamla, iptal et (`/checkout-sessions/...`).
- **Kargo**: Standart 9,99 $ veya Ekspres 24,99 $.
- **Sipariş sorgulama** (`GET /orders/{id}`).
- **Aynı isteğin iki kez işlenmemesi** (`Idempotency-Key` başlığı).
- İnsanlar için basit sayfalar: ana sayfa (`/`), ödeme devam sayfası (`/continue/...`), sipariş sayfası (`/order-status/...`), API belgesi (`/docs`).

### Sahte ödeme (mock payment)

Stripe Türkiye'de olmadığı için gerçek ödeme yok. Ajan ödeme gönderirken bir "test jetonu" (token) yazar,
mağaza sonucu buna göre seçer:

| Jeton | Ne olur |
| --- | --- |
| `success_token` | Ödeme onaylanır, sipariş oluşur. |
| `fail_token` | Reddedilir: bakiye yetersiz. |
| `decline_token` | Reddedilir: banka kartı reddetti. |
| `timeout_token` | Ödeme sistemi bir süre cevap vermez (varsayılan 15 sn), sonra başarısız olur. |
| `pending_token` | "İşleniyor" der, birkaç saniye sonra sipariş tamamlanır. |
| `fraud_token` | Şüpheli bulunur; alıcının onay sayfasından onaylaması gerekir. |

Ödeme bilgisi şu şekilde gönderilir:

```json
{"payment": {"instruments": [{
  "id": "pi_1", "handler_id": "mock_payment_handler", "type": "card", "selected": true,
  "credential": {"type": "token", "token": "success_token"}
}]}}
```

## Kendi bilgisayarında denemek

Gerekenler: Python 3.10 veya üstü.

```bash
# 1) Bir kere: sanal ortam kur ve gerekli paketleri yükle
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# 2) Mağazayı başlat (bu pencere açık kalsın)
uvicorn app.main:app --port 8080

# 3) Yeni bir pencerede test istemcisini çalıştır
python client/ucp_test_client.py
```

Tarayıcıda `http://localhost:8080` adresini açarsan ürün listesini görürsün.
Test istemcisinin sonunda `6/6 scenarios passed.` yazmalı.

Tek bir senaryo denemek için: `python client/ucp_test_client.py --scenario timeout`
Her isteği ve cevabı görmek için: `--verbose` ekle.

Otomatik testler: `pytest`

## Ayarlar (ortam değişkenleri)

| Ad | Varsayılan | Anlamı |
| --- | --- | --- |
| `BASE_URL` | boş (istekten bulunur) | Mağazanın herkese açık adresi. |
| `DB_PATH` | `data/store.db` | Veritabanı dosyası (SQLite). |
| `STORE_NAME` | `UCP Test Market` | Mağaza adı. |
| `STRICT_NEGOTIATION` | `false` | `true` olursa ajanın UCP tanıtım kartı zorunlu olur (kurallara tam uyum). |
| `ALLOW_INSECURE_PROFILE_URLS` | `false` | Sadece yerel deneme için: `http://` ajan adreslerine izin verir. |
| `MOCK_TIMEOUT_SECONDS` | `15` | `timeout_token` ne kadar beklesin. |
| `MOCK_PENDING_SECONDS` | `10` | `pending_token` kaç saniyede tamamlansın. |
| `ADMIN_TOKEN` | boş (kapalı) | Doluysa `POST /admin/reset` ile siparişler ve stok sıfırlanır. |

## Bilerek dışarıda bırakılanlar (ilk sürüm)

- **İmzalı istekler** (RFC 9421): UCP'de isteğe bağlı. Şimdilik kontrol edilmiyor.
- **MCP ve A2A bağlantı türleri**: sadece REST (normal web istekleri) var.
- **Sepet (cart), indirim kodu, vergi, sipariş bildirimi (webhook)**: sonraki sürümlere.
- **Kalıcı veritabanı**: Cloud Run'da SQLite dosyası sunucu yeniden başlayınca silinir.
  Test için sorun değil; kalıcı olsun istenirse ileride Cloud SQL veya Firestore'a geçilebilir.

## Cloud Run notu (henüz yüklenmedi)

Yükleme zamanı geldiğinde, veritabanı tek bir sunucuda dursun diye en fazla 1 kopya çalıştırılmalı:

```bash
gcloud run deploy ucp-test-market --source . --region europe-west1 \
  --project ucp-test-market --allow-unauthenticated --max-instances 1
```
