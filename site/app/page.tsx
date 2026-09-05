import { readFileSync } from "node:fs";
import { join } from "node:path";
import FarmaciasMap from "@/components/FarmaciasMap";
import { notFound } from "next/navigation";

export const dynamic = "force-dynamic";

type Establishment = {
  cnpj: string;
  cnpj_raw: string;
  matriz_filial: string;
  matriz_filial_code: string;
  razao_social: string;
  nome_fantasia: string;
  layer: "FARMACIA" | "COSMETICOS";
  layer_label: string;
  cnae_principal: string;
  cnae_secundaria: string;
  situacao: string;
  situacao_code: string;
  situacao_data: string;
  motivo_situacao: string;
  abertura_data: string;
  municipio_codigo: string;
  municipio_nome: string;
  uf: string;
  logradouro_tipo: string;
  logradouro: string;
  logradouro_full: string;
  numero: string;
  complemento: string;
  bairro: string;
  cep: string;
  telefone1: string;
  telefone2: string;
  email: string;
  capital_social: number | null;
  porte: string;
  simples: string;
  mei: string;
  lat: number | null;
  lon: number | null;
  _key: string;
};

type DataPayload = {
  cycle: string;
  generated_at: string;
  counts: {
    total: number;
    farmacia: number;
    cosmeticos: number;
    by_municipio: Record<string, number>;
    by_situacao: Record<string, number>;
  };
  geocoded: { count: number; total: number; coverage_pct: number };
  delta: {
    novas: number;
    baixas: number;
    mudaram_endereco: number;
    mudaram_situacao: number;
    total: number;
    rows: Array<{
      cnpj: string;
      cnpj_raw: string;
      kind: string;
      detail_json: string;
      detected_at: string;
      municipio_nome: string;
      razao_social: string;
      nome_fantasia: string;
      situacao: string;
      cnae_principal: string;
    }>;
  };
  establishments: Establishment[];
};

const MUNICIPALITY_ORDER = [
  "Santos",
  "São Vicente",
  "Guarujá",
  "Praia Grande",
  "Cubatão",
  "Bertioga",
  "Itanhaém",
  "Mongaguá",
  "Peruíbe"
];

function loadData(): DataPayload | null {
  const path = join(process.cwd(), "public", "data.json");
  try {
    const raw = readFileSync(path, "utf-8");
    return JSON.parse(raw) as DataPayload;
  } catch {
    return null;
  }
}

export default function Page() {
  const data = loadData();
  if (!data) {
    notFound();
  }

  const municipalities = Object.entries(data.counts.by_municipio)
    .map(([name, total]) => {
      const totalInCity = data.establishments.filter(
        (e) => e.municipio_nome === name
      ).length;
      const farmaciaInCity = data.establishments.filter(
        (e) => e.municipio_nome === name && e.layer === "FARMACIA"
      ).length;
      return { name, total: totalInCity, farmacia: farmaciaInCity };
    })
    .sort((a, b) => {
      const ia = MUNICIPALITY_ORDER.indexOf(a.name);
      const ib = MUNICIPALITY_ORDER.indexOf(b.name);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    });

  return (
    <main className="flex min-h-screen flex-col">
      <header className="border-b border-ink-200 bg-white">
        <div className="mx-auto flex max-w-screen-2xl flex-col gap-3 px-4 py-5 sm:px-6 sm:py-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-brand-700">
              <span className="inline-flex h-2 w-2 rounded-full bg-brand-500 shadow-ring" />
              Sindicato das Farmácias da Baixada Santista
            </div>
            <h1 className="text-2xl font-extrabold tracking-tight text-ink-900 sm:text-3xl">
              Mapa de Farmácias — Baixada Santista
            </h1>
            <p className="max-w-2xl text-sm text-ink-600">
              Estabelecimentos dos 9 municípios (Santos, São Vicente, Guarujá, Praia Grande, Cubatão,
              Bertioga, Itanhaém, Mongaguá e Peruíbe), com filtro por município, categoria,
              situação cadastral e tipo (matriz/filial). Base oficial da Receita Federal,
              atualizada mensalmente.
            </p>
          </div>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Estabelecimentos" value={data.counts.total.toLocaleString("pt-BR")} accent="brand" />
            <Stat label="Farmácias" value={data.counts.farmacia.toLocaleString("pt-BR")} accent="brand" />
            <Stat label="Cosméticos/Perfumaria" value={data.counts.cosmeticos.toLocaleString("pt-BR")} accent="accent" />
            <Stat
              label="Cobertura geográfica"
              value={`${data.geocoded.coverage_pct.toFixed(1)}%`}
              sub={`${data.geocoded.count.toLocaleString("pt-BR")} / ${data.geocoded.total.toLocaleString("pt-BR")} no mapa`}
              accent="ink"
            />
          </dl>
        </div>
      </header>

      <FarmaciasMap data={data} municipalities={municipalities} />

      <footer className="border-t border-ink-200 bg-white py-4 text-center text-xs text-ink-500">
        Ciclo {data.cycle} • Atualizado em {new Date(data.generated_at).toLocaleString("pt-BR")} •
        Tiles via OpenStreetMap contributors
      </footer>
    </main>
  );
}

function Stat({
  label,
  value,
  sub,
  accent = "brand"
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: "brand" | "accent" | "ink";
}) {
  const accentClass =
    accent === "brand"
      ? "bg-brand-50 text-brand-800 border-brand-100"
      : accent === "accent"
      ? "bg-accent-50 text-accent-800 border-accent-100"
      : "bg-ink-100 text-ink-800 border-ink-200";
  return (
    <div className={`rounded-xl border px-3 py-2 ${accentClass}`}>
      <div className="text-[10px] font-semibold uppercase tracking-wide opacity-80">{label}</div>
      <div className="text-base font-bold tabular-nums sm:text-lg">{value}</div>
      {sub && <div className="text-[10px] opacity-70">{sub}</div>}
    </div>
  );
}