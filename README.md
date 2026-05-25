# 🏭 Livinghouse · Bot de Control de Producción

Bot de Telegram para gestionar entregas de contratistas, verificación de productos y generación de cuentas de cobro.

---

## ¿Qué hace este bot?

| Actor | Puede hacer |
|---|---|
| **Contratista** (Joselyn, etc.) | Reportar productos terminados con foto y FVE |
| **Verificador** (Cindy, Juan David) | Aprobar/rechazar/ajustar precio de cada entrega |
| **Admin** | Consultar resúmenes, gestionar precios desde el dashboard |

---

## Flujo de una entrega

```
Contratista               Bot                    Verificador
    │                      │                          │
    │── /reportar ─────────▶│                          │
    │◀─ ¿FVE? ─────────────│                          │
    │── FVE 2118 ───────────▶│                          │
    │◀─ ¿Producto? ─────────│                          │
    │── Sofa Cama... ────────▶│                          │
    │◀─ [Lista] [Especial] ──│                          │
    │── [Lista] ─────────────▶│                          │
    │◀─ Precio: $85.000 ─────│                          │
    │── 📸 Foto ─────────────▶│                          │
    │◀─ ✅ Reporte enviado ──│── Foto + datos ─────────▶│
    │                      │                    [✅][❌]│
    │◀──────────────────────│◀── ✅ Aprobado ───────────│
    │  ✅ ¡Aprobado!        │                          │
```

---

## Instalación paso a paso

### Paso 1 — Crear el bot en Telegram

1. Abre Telegram y busca **@BotFather**
2. Envíale `/newbot`
3. Ponle un nombre: `Livinghouse Producción`
4. Ponle un username: `livinghouse_prod_bot` (o el que esté disponible)
5. BotFather te dará un **token** — cópialo, lo necesitarás

### Paso 2 — Crear la base de datos en Supabase

1. Ve a [supabase.com](https://supabase.com) y crea una cuenta gratis
2. Crea un nuevo proyecto (ponle nombre: `livinghouse`)
3. En el panel izquierdo ve a **SQL Editor → New Query**
4. Copia y pega todo el contenido de `schema.sql`
5. Haz clic en **Run** — se crearán todas las tablas
6. Ve a **Settings → API** y copia:
   - `Project URL` → es tu `SUPABASE_URL`
   - `anon public key` → es tu `SUPABASE_KEY`

### Paso 3 — Registrar a Cindy y Juan David

Para saber el ID de Telegram de cada persona:
1. Pídeles que le escriban `/start` al bot @userinfobot en Telegram
2. Ese número es su `telegram_id`

Luego en Supabase → **Table Editor → verifiers** → **Insert row**:
```
name: Cindy
telegram_id: [el número que les dio @userinfobot]
active: true
```

Repite para Juan David.

### Paso 4 — Registrar a los contratistas

En Supabase → **Table Editor → workers** → **Insert row**:
```
name: Joselyn
area: costura        ← opciones: tapicería, esqueletería, pintura, costura
telegram_id: [su número de Telegram]
activo: true
```

### Paso 5 — Instalar y correr el bot

**Requisito:** tener Python 3.10+ instalado.

```bash
# 1. Descomprime la carpeta del proyecto
cd livinghouse-bot

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Crear el archivo de configuración
cp .env.example .env

# 4. Editar .env con tus datos reales
# Abre .env con cualquier editor de texto y rellena:
# BOT_TOKEN=el token de BotFather
# SUPABASE_URL=la URL de tu proyecto
# SUPABASE_KEY=la clave anon de Supabase

# 5. Correr el bot
python bot.py
```

Deberías ver: `🏭 Bot Livinghouse iniciado y escuchando...`

---

## Comandos disponibles

### Para contratistas
| Comando | Qué hace |
|---|---|
| `/start` | Bienvenida e información |
| `/reportar` | Registrar un producto terminado |
| `/mistotal` | Ver acumulado de la semana actual |
| `/cancelar` | Cancelar un reporte en curso |

### Para verificadores (Cindy / Juan David)
| Comando | Qué hace |
|---|---|
| `/pendientes` | Ver todas las entregas sin aprobar |
| `/resumen Joselyn semana` | Cuenta de cobro de la última semana |
| `/resumen Joselyn quincena` | Cuenta de cobro de la última quincena |
| `/precios sofa cama` | Buscar precios en la lista maestra |

---

## Importar la lista de precios desde Excel

1. Ve a Supabase → **SQL Editor**
2. Para cada producto, ejecuta:

```sql
INSERT INTO price_list (product_name, area, precio_corte, precio_costura, precio_total)
VALUES ('SOFA CAMA MONTREAL TIPO 2 ROYAL FACTORY', 'costura', 15000, 70000, 85000);
```

O importa el CSV directamente desde **Table Editor → Import CSV**.

---

## Correr en producción (gratis)

Para que el bot corra 24/7 sin tener tu computador encendido:

1. Crea cuenta en [railway.app](https://railway.app)
2. Crea un nuevo proyecto → Deploy from GitHub
3. Sube este código a un repositorio privado de GitHub
4. En Railway, ve a **Variables** y agrega las 3 variables del `.env`
5. Railway detecta automáticamente que es Python y lo corre

---

## Próximos pasos (Etapa 2)

- [ ] Dashboard web para ver reportes y gestionar precios
- [ ] Exportar cuentas de cobro a Excel/PDF desde Telegram
- [ ] Alertas de discrepancias de precio
- [ ] Historial completo por contratista

---

_Desarrollado para Livinghouse · Sistema de control de producción_
