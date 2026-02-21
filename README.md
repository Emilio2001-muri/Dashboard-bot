# NEXUS AI Trading Terminal — Vercel + Supabase

Dashboard de trading en tiempo real desplegado en **Vercel** con datos almacenados en **Supabase**.

## Arquitectura

```
┌──────────────┐      push data     ┌──────────────┐     realtime      ┌──────────────┐
│  Tu PC local │  ──────────────▶   │   Supabase   │  ◀────────────▶  │    Vercel     │
│  pusher.py   │   (service key)    │   Postgres   │   (anon key)     │  index.html   │
│  + MT5       │                    │   + Realtime  │                  │  (estático)   │
└──────────────┘                    └──────────────┘                  └──────────────┘
```

- **pusher.py** corre en tu PC (donde MT5 está instalado), recolecta datos y los sube a Supabase cada pocos segundos.
- **Supabase** almacena los datos en una tabla `dashboard_cache` y los envía al frontend via Realtime (websockets).
- **Vercel** sirve el HTML/CSS/JS estático — no necesita servidor backend.

---

## Setup paso a paso

### 1. Crear proyecto en Supabase

1. Ve a [supabase.com](https://supabase.com) → **New Project**
2. Elige un nombre (ej: `nexus-dashboard`) y una contraseña
3. Espera a que se cree (~2 min)

### 2. Crear la tabla

1. En Supabase → **SQL Editor** → **New Query**
2. Pega el contenido de `supabase_schema.sql` y ejecuta (Run)
3. Verifica en **Table Editor** que existe `dashboard_cache` con 14 filas

### 3. Obtener las keys

En Supabase → **Settings** → **API**:

| Key | Para qué | Dónde va |
|-----|----------|----------|
| **Project URL** | Conexión | `public/index.html` + `local/.env` |
| **anon (public)** | Lee datos desde frontend | `public/index.html` |
| **service_role** | Escribe datos desde pusher | `local/.env` (NUNCA en frontend) |

### 4. Configurar el frontend

Edita `public/index.html` — busca estas líneas al inicio del `<script>`:

```js
const SUPABASE_URL = 'https://YOUR_PROJECT.supabase.co';       // ← Tu Project URL
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6...';  // ← Tu anon key
```

### 5. Configurar el pusher local

```bash
cd local
copy .env.example .env
```

Edita `local/.env`:

```
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGci...tu_service_role_key_aqui
```

Instala dependencias:

```bash
pip install -r requirements.txt
```

### 6. Subir a GitHub + Vercel

```bash
cd Dashboard-Vercel

git init
git add .
git commit -m "NEXUS Trading Dashboard v1.0"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/nexus-dashboard.git
git push -u origin main
```

Luego en [vercel.com](https://vercel.com):
1. **Import Project** → selecciona tu repo de GitHub
2. Vercel detecta automáticamente `vercel.json`
3. Click **Deploy** → listo, tu dashboard está online

### 7. Ejecutar el pusher

En tu PC (donde MT5 está corriendo):

```bash
cd local
python pusher.py
```

O simplemente doble-click en `START_PUSHER.bat`.

El pusher empezará a recolectar datos de MT5 + APIs y los subirá a Supabase. El dashboard en Vercel se actualizará automáticamente via Realtime.

---

## Estructura de archivos

```
Dashboard-Vercel/
├── public/
│   └── index.html          ← Frontend (Vercel lo sirve)
├── local/
│   ├── pusher.py            ← Data collector (corre en tu PC)
│   ├── requirements.txt     ← Dependencias Python
│   ├── .env.example         ← Template de configuración
│   └── START_PUSHER.bat     ← Ejecutar pusher con doble-click
├── supabase_schema.sql      ← SQL para crear tabla en Supabase
├── vercel.json              ← Config de Vercel
├── .gitignore               ← Ignora .env y archivos locales
└── README.md                ← Este archivo
```

## Notas importantes

- **NUNCA** pongas el `service_role key` en el frontend — solo el `anon key` (público, read-only)
- El `local/.env` está en `.gitignore`, nunca se sube a GitHub
- Si Realtime no funciona, verifica que ejecutaste el `ALTER PUBLICATION` del schema SQL
- El pusher necesita que MT5 esté abierto y logueado en tu PC
- Los intervalos de actualización: MT5 cada 10s, precios 90s, noticias 5min, calendario 10min
