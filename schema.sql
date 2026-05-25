-- ================================================================
-- LIVINGHOUSE · SISTEMA DE CONTROL DE PRODUCCIÓN
-- Schema de base de datos para Supabase (PostgreSQL)
-- Ejecutar en: Supabase → SQL Editor → New Query
-- ================================================================

-- ─── ÁREAS DE PRODUCCIÓN ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS areas (
  id        SERIAL PRIMARY KEY,
  nombre    TEXT NOT NULL UNIQUE,  -- tapicería, esqueletería, pintura, costura
  activo    BOOLEAN DEFAULT TRUE
);

INSERT INTO areas (nombre) VALUES
  ('tapicería'),
  ('esqueletería'),
  ('pintura'),
  ('costura')
ON CONFLICT DO NOTHING;

-- ─── TRABAJADORES (CONTRATISTAS) ──────────────────────────────
CREATE TABLE IF NOT EXISTS workers (
  id            SERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  telegram_id   BIGINT UNIQUE,          -- ID de Telegram del contratista
  area          TEXT REFERENCES areas(nombre),
  phone         TEXT,
  activo        BOOLEAN DEFAULT TRUE,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ─── VERIFICADORES (Cindy, Juan David) ────────────────────────
CREATE TABLE IF NOT EXISTS verifiers (
  id            SERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  telegram_id   BIGINT UNIQUE NOT NULL,
  active        BOOLEAN DEFAULT TRUE,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ─── LISTA MAESTRA DE PRECIOS ─────────────────────────────────
CREATE TABLE IF NOT EXISTS price_list (
  id              SERIAL PRIMARY KEY,
  product_name    TEXT NOT NULL,
  area            TEXT,                  -- área a la que aplica este precio
  precio_corte    NUMERIC(12,2),         -- precio de corte (puede ser NULL)
  precio_costura  NUMERIC(12,2),         -- precio de costura (puede ser NULL)
  precio_total    NUMERIC(12,2) NOT NULL,
  notas           TEXT,
  active          BOOLEAN DEFAULT TRUE,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_by      TEXT                   -- quién actualizó el precio
);

-- Índice para búsqueda de productos por nombre
CREATE INDEX IF NOT EXISTS idx_price_list_name ON price_list USING gin(to_tsvector('spanish', product_name));

-- ─── ENTREGAS (el corazón del sistema) ────────────────────────
CREATE TABLE IF NOT EXISTS deliveries (
  id                      SERIAL PRIMARY KEY,
  worker_id               INT REFERENCES workers(id) ON DELETE RESTRICT,
  fve                     TEXT NOT NULL,             -- número FVE / ODP
  product_name            TEXT NOT NULL,             -- nombre del producto
  price_list_id           INT REFERENCES price_list(id),  -- precio de lista (NULL si especial)
  is_special              BOOLEAN DEFAULT FALSE,     -- ¿precio especial?
  requested_price         NUMERIC(12,2),             -- precio que pidió el contratista
  final_price             NUMERIC(12,2) NOT NULL,    -- precio final aprobado
  photo_file_id           TEXT,                      -- file_id de Telegram
  photo_url               TEXT,                      -- URL de la foto
  status                  TEXT DEFAULT 'pending'     -- pending | approved | rejected
                          CHECK (status IN ('pending','approved','rejected')),
  rejection_reason        TEXT,
  approved_by             TEXT,                      -- nombre del verificador
  approved_by_telegram_id BIGINT,
  approved_at             TIMESTAMPTZ,
  notes                   TEXT,
  created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Índices para consultas frecuentes
CREATE INDEX IF NOT EXISTS idx_deliveries_worker    ON deliveries(worker_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_status    ON deliveries(status);
CREATE INDEX IF NOT EXISTS idx_deliveries_created   ON deliveries(created_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_fve       ON deliveries(fve);

-- ─── PERIODOS DE PAGO ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payment_periods (
  id              SERIAL PRIMARY KEY,
  worker_id       INT REFERENCES workers(id),
  period_label    TEXT NOT NULL,             -- ej: "Semana 15-21 Ene 2026"
  start_date      DATE NOT NULL,
  end_date        DATE NOT NULL,
  total_amount    NUMERIC(12,2),             -- calculado al cerrar
  social_security NUMERIC(12,2),            -- descuento seguridad social
  net_payment     NUMERIC(12,2),            -- total - descuentos
  status          TEXT DEFAULT 'open'       -- open | closed | paid
                  CHECK (status IN ('open','closed','paid')),
  closed_at       TIMESTAMPTZ,
  paid_at         TIMESTAMPTZ,
  notes           TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── VISTA: RESUMEN POR TRABAJADOR ────────────────────────────
CREATE OR REPLACE VIEW worker_summary AS
SELECT
  w.name AS worker_name,
  w.area,
  w.telegram_id,
  COUNT(d.id) FILTER (WHERE d.status = 'approved') AS total_approved,
  COUNT(d.id) FILTER (WHERE d.status = 'pending')  AS total_pending,
  COUNT(d.id) FILTER (WHERE d.status = 'rejected') AS total_rejected,
  COALESCE(SUM(d.final_price) FILTER (WHERE d.status = 'approved'), 0) AS total_earned,
  MAX(d.created_at) AS last_delivery
FROM workers w
LEFT JOIN deliveries d ON d.worker_id = w.id
WHERE w.activo = TRUE
GROUP BY w.id, w.name, w.area, w.telegram_id;

-- ─── TRIGGER: Actualizar updated_at en price_list ─────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER price_list_updated_at
  BEFORE UPDATE ON price_list
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ================================================================
-- DATOS DE EJEMPLO (eliminar en producción)
-- ================================================================

-- Verificadores
INSERT INTO verifiers (name, telegram_id) VALUES
  ('Cindy',      123456789),   -- reemplazar con IDs reales de Telegram
  ('Juan David', 987654321)
ON CONFLICT DO NOTHING;

-- Trabajadora de ejemplo
INSERT INTO workers (name, area, telegram_id) VALUES
  ('Joselyn', 'costura', 111111111)  -- reemplazar con ID real
ON CONFLICT DO NOTHING;

-- Precios de ejemplo (el resto se importa desde el Excel)
INSERT INTO price_list (product_name, area, precio_corte, precio_costura, precio_total) VALUES
  ('SOFA CAMA MONTREAL TIPO 2 ROYAL FACTORY', 'costura', 15000, 70000, 85000),
  ('SOFA CAMA VICTORIA 2 PUESTOS ROYAL FACTORY 1.75', 'costura', 30000, 100000, 130000),
  ('SALA VICTORIA', 'costura', 50000, 130000, 180000),
  ('SALA OLIMPO', 'costura', 50000, 150000, 200000),
  ('SILLA TOKIO PATA TORNEADA VANEGAS', 'costura', 5000, 20000, 25000),
  ('SILLA MEDIA LUNA VANEGAS', 'costura', 5000, 20000, 25000),
  ('SILLA ROMBO JANETTE', 'costura', 6000, 22000, 28000),
  ('SOFA TUENDY 3 PUESTOS MELANY 2.20 MTS', 'costura', 40000, 110000, 150000)
ON CONFLICT DO NOTHING;
