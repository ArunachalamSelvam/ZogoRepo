# LOHO — Setup & Deployment Guide

## Local Development (SQLite — zero config)

```bash
cd /Users/arun-21785/Downloads/LOHO
python3 server.py
```

- Server: `http://localhost:8000`
- Game: `http://localhost:8000/Zoho-Logo2.html`
- Leaderboard: `http://localhost:8000/leaderboard.html`
- API: `http://localhost:8000/api/results`
- Database: `data/quiz_results.db` (auto-created)

No environment variables needed — SQLite is used automatically.

---

## Production: Render + Firebase Firestore (recommended)

### Step 1 — Create a Firebase project & Firestore database

1. Go to [Firebase Console](https://console.firebase.google.com/) → **Add project**
2. Name it (e.g. `loho-quiz`) → Continue → Create Project
3. In the sidebar: **Build → Firestore Database → Create database**
   - Start in **production mode**
   - Pick a Cloud Firestore location close to your users
4. Go to **Project Settings → Service accounts → Generate new private key**
5. A JSON file downloads. This is your service account credential.

### Step 2 — Encode the credentials for Render

Render env vars work best as a single string. Choose one method:

**Option A — Raw JSON (simpler)**

Open the downloaded JSON file, copy the entire content, and paste it as the
`FIREBASE_CREDENTIALS` env var value on Render (Render supports multi-line values).

**Option B — Base64 (safer for CI/CD)**

```bash
base64 -i path/to/serviceAccountKey.json | tr -d '\n'
```

Copy the output — that's your `FIREBASE_CREDENTIALS` value.

### Step 3 — Push your code to GitHub

```bash
cd /Users/arun-21785/Downloads/LOHO
git init
git add .
git commit -m "Initial commit — LOHO quiz game"
git remote add origin https://github.com/YOUR_USERNAME/LOHO.git
git push -u origin main
```

### Step 4 — Deploy on Render

1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your GitHub repo
3. Configure:
   - **Name**: `loho-quiz`
   - **Runtime**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python server.py`
4. Add environment variables:
   - `FIREBASE_CREDENTIALS` = paste the JSON (or base64 string) from Step 2
   - `PORT` = `8000`
5. Click **Create Web Service**

Your app will be live at:

```
https://loho-quiz.onrender.com
```

### Step 5 — Set Firestore security rules

In Firebase Console → Firestore → Rules, set:

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /{document=**} {
      allow read, write: if false;
    }
  }
}
```

This locks down direct client access (all access goes through your server).

---

## How the server picks a database

| Priority | Environment variable           | Database used          |
|----------|-------------------------------|------------------------|
| 1st      | `FIREBASE_CREDENTIALS` is set | Firebase Firestore     |
| 2nd      | `DATABASE_URL` starts with `postgres` | PostgreSQL    |
| 3rd      | Neither is set                | SQLite (local file)    |

---

## Alternative: Render + Neon PostgreSQL

If you prefer PostgreSQL over Firebase:

1. Sign up at [neon.tech](https://neon.tech) (free)
2. Create a project → copy the connection string
3. On Render, set `DATABASE_URL` = the Neon connection string (don't set `FIREBASE_CREDENTIALS`)
4. The server will auto-detect and use PostgreSQL

---

## API Reference

| Method   | Endpoint              | Description                               |
|----------|-----------------------|-------------------------------------------|
| `GET`    | `/api/results`        | List results (`?limit=20&sort=leaderboard`) |
| `GET`    | `/api/results?limit=all` | All rows                               |
| `POST`   | `/api/results`        | Save a game result                        |
| `DELETE` | `/api/results`        | Clear all records                         |
| `GET`    | `/api/results/stream` | SSE live updates                          |

### Stored fields

- `playerName`, `playerEmail`
- `score`, `correctCount`, `totalAnswered`, `accuracy`
- `outcome` (`victory` or `game_over`)
- `createdAt`
