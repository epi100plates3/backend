# 🎬 YT Downloader – Progressive MP4 YouTube Downloader

Aplikacja webowa do pobierania filmów z YouTube jako **gotowe pliki MP4** (progressive download), **bez ffmpeg, bez konwersji, bez łączenia strumieni**.

## 🏗️ Architektura

```
┌─────────────────────┐         ┌─────────────────────┐
│   Frontend          │  fetch  │   Backend (Render)   │
│   epii.pl/yt        │ ◄─────► │   FastAPI + yt-dlp   │
│   HTML/CSS/JS       │   API   │   Python 3.11        │
└─────────────────────┘         └─────────────────────┘
```

## 📁 Struktura projektu

```
ytdown/
├── backend/
│   ├── main.py            # FastAPI application
│   ├── requirements.txt   # Python dependencies
│   └── render.yaml        # Render.com deployment config
├── frontend/
│   ├── index.html         # Main page
│   ├── style.css          # Styles (dark theme)
│   └── script.js          # Frontend logic (vanilla JS)
└── README.md
```

## 🚀 Deploy – Backend (Render.com)

### Krok 1: Przygotuj repozytorium GitHub

1. Stwórz nowe repozytorium na GitHub
2. Wrzuć do niego zawartość folderu `backend/`:
   - `main.py`
   - `requirements.txt`
   - `render.yaml`
   - `cookies.txt` (wyeksportowane ciasteczka YouTube – patrz niżej)
   - `.python-version`

### Krok 1b: Wyeksportuj ciasteczka YouTube (wymagane!)

Bez cookies YouTube blokuje requesty z serwerów chmurowych.

1. Zainstaluj rozszerzenie: **[Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)**
2. Zaloguj się na YouTube w przeglądarce
3. Kliknij ikonkę rozszerzenia → **Export**
4. Zapisz jako `cookies.txt`
5. Skopiuj do folderu `backend/` i dodaj do repozytorium

### Krok 2: Deploy na Render.com

1. Zaloguj się na [Render.com](https://render.com)
2. Kliknij **New +** → **Web Service**
3. Połącz swoje repozytorium GitHub
4. Render **automatycznie wykryje `render.yaml`** i skonfiguruje:
   - Runtime: Python 3
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Kliknij **Create Web Service**
6. Po deployu otrzymasz URL w stylu: `https://ytdown-api.onrender.com`

### Krok 3: Test backendu

```bash
# Health check
curl https://twoj-backend.onrender.com/health

# Test info endpoint
curl -X POST https://twoj-backend.onrender.com/api/info \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

## 🌐 Deploy – Frontend (epii.pl/yt)

1. Skopiuj zawartość folderu `frontend/` na serwer:
   ```
   /public_html/yt/
   ├── index.html
   ├── style.css
   └── script.js
   ```

2. **WAŻNE**: W pliku `script.js` zmień `API_BASE` na URL swojego backendu Render:

```javascript
const API_BASE = "https://backend-vez9.onrender.com";  // ← Twój backend
```

3. Wgraj pliki przez FTP na `epii.pl` do katalogu `/yt/`

## 🔧 Lokalne testowanie

### Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Backend będzie dostępny na `http://localhost:8000`
Dokumentacja Swagger: `http://localhost:8000/docs`

### Frontend

W `script.js` odkomentuj lokalny API_BASE:
```javascript
const API_BASE = "http://localhost:8000";
```

Otwórz `index.html` w przeglądarce (lub użyj Live Server).

## ⚠️ Ograniczenia

| ✅ Działa | ❌ Nie działa |
|-----------|---------------|
| Progressive MP4 (360p, 720p) | 1080p+ (adaptive only) |
| Format 18, 22 | MP3 / audio-only |
| Pojedynczy plik MP4 | Łączenie video+audio |
| Bez ffmpeg | Konwersja formatów |
| Max 720p | Filmy >500 MB |

## 🔌 API Endpoints

### `GET /health`
Health check dla Render.com.

### `POST /api/info`
Pobiera informacje o filmie.

**Request:**
```json
{ "url": "https://www.youtube.com/watch?v=..." }
```

**Response:**
```json
{
  "success": true,
  "title": "Tytuł filmu",
  "thumbnail": "https://...",
  "duration": 213,
  "uploader": "Nazwa kanału",
  "formats": [
    { "format_id": "18", "quality": "360p", "resolution": "640x360", "filesize": 12345678 }
  ]
}
```

### `POST /api/download`
Pobiera plik MP4.

**Request:**
```json
{ "url": "https://...", "format_id": "18" }
```

**Response:** Plik MP4 (binary stream)

## 🛡️ Bezpieczeństwo

- Rate limiting: 10 req/min na `/api/info`, 5 req/min na `/api/download`
- Walidacja URL YouTube
- Limit rozmiaru pliku: 500 MB
- Timeout download: 120 sekund
- CORS tylko dla `epii.pl`
- Pliki tymczasowe usuwane automatycznie

## 📝 Licencja

MIT – używaj odpowiedzialnie, zgodnie z ToS YouTube.
