"use client";

import { useEffect, useMemo, useRef, useState } from "react";

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

type DeltaRow = {
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
    rows: DeltaRow[];
  };
  establishments: Establishment[];
};

const MUNICIPALITY_CENTERS: Record<string, [number, number]> = {
  Santos: [-23.9619, -46.3322],
  "São Vicente": [-23.9608, -46.3922],
  Guarujá: [-23.9939, -46.2562],
  "Praia Grande": [-24.0058, -46.4028],
  Cubatão: [-23.8956, -46.4244],
  Bertioga: [-23.8547, -46.1386],
  Itanhaém: [-24.1833, -46.7889],
  Mongaguá: [-24.0933, -46.6206],
  Peruíbe: [-24.32, -46.9989]
};

const BAIXADA_CENTER: [number, number] = [-23.95, -46.45];

type FilterState = {
  municipio: string;
  camada: "ALL" | "FARMACIA" | "COSMETICOS";
  situacao: string;
  tipo: "ALL" | "MATRIZ" | "FILIAL";
  onlyChanges: boolean;
  search: string;
};

const DEFAULT_FILTERS: FilterState = {
  municipio: "ALL",
  camada: "ALL",
  situacao: "ALL",
  tipo: "ALL",
  onlyChanges: false,
  search: ""
};

export default function FarmaciasMap({
  data,
  municipalities
}: {
  data: DataPayload;
  municipalities: Array<{ name: string; total: number; farmacia: number }>;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<any>(null);
  const clusterRef = useRef<any>(null);
  const [mapReady, setMapReady] = useState(false);
  const deltaByCnpj = useMemo(() => {
    const m = new Map<string, DeltaRow>();
    for (const r of data.delta.rows) m.set(r.cnpj_raw, r);
    return m;
  }, [data.delta.rows]);

  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const [selected, setSelected] = useState<Establishment | null>(null);
  const [showChanges, setShowChanges] = useState(false);
  const [legendDismissed, setLegendDismissed] = useState(false);

  const situacoes = useMemo(() => {
    return Object.keys(data.counts.by_situacao).filter(Boolean).sort();
  }, [data.counts.by_situacao]);

  const filtered = useMemo(() => {
    const q = filters.search.trim().toLowerCase();
    return data.establishments.filter((e) => {
      if (filters.municipio !== "ALL" && e.municipio_nome !== filters.municipio) return false;
      if (filters.camada !== "ALL" && e.layer !== filters.camada) return false;
      if (filters.situacao !== "ALL" && e.situacao !== filters.situacao) return false;
      if (filters.tipo !== "ALL" && e.matriz_filial_code !== (filters.tipo === "MATRIZ" ? "1" : "2"))
        return false;
      if (filters.onlyChanges && !deltaByCnpj.has(e.cnpj_raw)) return false;
      if (q) {
        const hay = [
          e.razao_social,
          e.nome_fantasia,
          e.cnpj,
          e.logradouro_full,
          e.bairro,
          e.municipio_nome,
          e.cep
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [data.establishments, filters, deltaByCnpj]);

  const filteredWithCoords = useMemo(
    () => filtered.filter((e) => e.lat != null && e.lon != null),
    [filtered]
  );

  // Mount Leaflet once.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      const L = (await import("leaflet")).default;
      // markercluster extends L on import
      await import("leaflet.markercluster");
      if (cancelled || !containerRef.current) return;

      // Fix default icon path (CDN bundled)
      delete (L.Icon.Default.prototype as any)._getIconUrl;
      L.Icon.Default.mergeOptions({
        iconRetinaUrl:
          "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
        iconUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
        shadowUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png"
      });

      const map = L.map(containerRef.current, {
        center: BAIXADA_CENTER,
        zoom: 10,
        zoomControl: true,
        preferCanvas: true,
        attributionControl: true
      });

      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "© OpenStreetMap contributors"
      }).addTo(map);

      // Baixada outline (rough) — fits the focus area
      const bbox: [number, number, number, number] = [-47.2, -24.5, -46.0, -23.7];
      map.fitBounds([
        [bbox[1], bbox[0]],
        [bbox[3], bbox[2]]
      ]);

      mapRef.current = map;
      clusterRef.current = (L as any).markerClusterGroup({
        chunkedLoading: true,
        chunkInterval: 120,
        chunkDelay: 30,
        showCoverageOnHover: false,
        spiderfyOnMaxZoom: true,
        maxClusterRadius: 50,
        disableClusteringAtZoom: 16,
        iconCreateFunction: (c: any) => {
          const count = c.getChildCount();
          const size = count < 25 ? "small" : count < 200 ? "medium" : "large";
          const cls = `marker-cluster marker-cluster-${size}`;
          const sizePx = size === "small" ? 36 : size === "medium" ? 44 : 52;
          return L.divIcon({
            html: `<div class="${cls}" style="width:${sizePx}px;height:${sizePx}px;border-radius:999px;display:flex;align-items:center;justify-content:center;color:white;font-weight:700;box-shadow:0 6px 14px -6px rgba(15,23,42,0.35);border:2px solid rgba(255,255,255,0.85)"><span>${count}</span></div>`,
            className: "marker-cluster-wrapper",
            iconSize: L.point(sizePx, sizePx)
          });
        }
      });
      map.addLayer(clusterRef.current);
      setMapReady(true);
    })();

    return () => {
      cancelled = true;
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
      clusterRef.current = null;
    };
  }, []);

  // Re-render cluster whenever filtered set changes (or map becomes ready).
  useEffect(() => {
    if (!mapReady) return;
    (async () => {
      if (!clusterRef.current || !mapRef.current) return;
      const L = (await import("leaflet")).default;
      const cluster = clusterRef.current;

      cluster.clearLayers();

      const layerIconCache = new Map<string, any>();
      const layerColor = (layer: string) =>
        layer === "FARMACIA" ? "#1f9270" : "#f56d18";
      const layerShadow = (layer: string) =>
        layer === "FARMACIA" ? "rgba(15,93,73,0.4)" : "rgba(178,54,12,0.45)";

      const markers: any[] = [];
      const fragment = document.createDocumentFragment();
      for (const e of filteredWithCoords) {
        const key = e.layer;
        let icon = layerIconCache.get(key);
        if (!icon) {
          icon = L.divIcon({
            className: "estab-marker",
            iconSize: [22, 28],
            iconAnchor: [11, 26],
            popupAnchor: [0, -24],
            html: `<div class="anim-pin" style="
              width:22px;height:28px;position:relative;
              filter: drop-shadow(0 4px 6px ${layerShadow(key)});
            ">
              <svg viewBox="0 0 24 32" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">
                <path d="M12 0C5.4 0 0 5.4 0 12c0 8 12 20 12 20s12-12 12-20C24 5.4 18.6 0 12 0z" fill="${layerColor(key)}"/>
                <circle cx="12" cy="12" r="5" fill="#ffffff"/>
                <circle cx="12" cy="12" r="2.5" fill="${layerColor(key)}"/>
              </svg>
            </div>`
          });
          layerIconCache.set(key, icon);
        }
        const m = L.marker([e.lat!, e.lon!], { icon });
        m.bindTooltip(
          `<strong>${escapeHtml(e.nome_fantasia || e.razao_social)}</strong><br>${escapeHtml(e.logradouro_full)} ${escapeHtml(e.numero || "")}, ${escapeHtml(e.municipio_nome)}`,
          { direction: "top", offset: [0, -22], className: "estab-tooltip" }
        );
        m.on("click", () => setSelected(e));
        markers.push(m);
      }
      cluster.addLayers(markers);
    })();
  }, [filteredWithCoords, mapReady]);

  const cycleChangeKinds = useMemo(() => {
    const set = new Set<string>();
    for (const r of data.delta.rows) set.add(r.kind);
    return set;
  }, [data.delta.rows]);

  const filteredDeltaRows = useMemo(() => {
    return data.delta.rows.filter((r) => {
      if (filters.municipio !== "ALL" && r.municipio_nome !== filters.municipio) return false;
      if (filters.camada !== "ALL") {
        const inEstab = data.establishments.find((e) => e.cnpj_raw === r.cnpj_raw);
        if (!inEstab || inEstab.layer !== filters.camada) return false;
      }
      if (filters.search.trim()) {
        const q = filters.search.trim().toLowerCase();
        const hay = [r.razao_social, r.nome_fantasia, r.cnpj, r.municipio_nome]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [data.delta.rows, data.establishments, filters.municipio, filters.camada, filters.search]);

  function resetFilters() {
    setFilters(DEFAULT_FILTERS);
  }

  function focusMunicipality(name: string) {
    if (!mapRef.current) return;
    const center = MUNICIPALITY_CENTERS[name];
    if (center) {
      mapRef.current.flyTo(center, 13, { duration: 0.9 });
    }
  }

  return (
    <div className="relative grid flex-1 grid-cols-1 lg:grid-cols-[320px_1fr]">
      {/* Sidebar — desktop */}
      <aside className="hidden flex-col gap-4 border-r border-ink-200 bg-white p-4 lg:flex">
        <SidebarFilters
          filters={filters}
          setFilters={setFilters}
          situacoes={situacoes}
          municipalities={municipalities}
          resetFilters={resetFilters}
          totalShown={filtered.length}
          totalWithCoords={filteredWithCoords.length}
          deltaSummary={data.delta}
          cycleChangeKinds={Array.from(cycleChangeKinds)}
          showChanges={showChanges}
          setShowChanges={setShowChanges}
          onFocusMunicipality={focusMunicipality}
        />
      </aside>

      {/* Map */}
      <div className="relative">
        <div ref={containerRef} className="absolute inset-0" aria-label="Mapa interativo" />

        {/* Floating filter button (mobile) */}
        <MobileBar
          filters={filters}
          setFilters={setFilters}
          situacoes={situacoes}
          municipalities={municipalities}
          totalShown={filtered.length}
          totalWithCoords={filteredWithCoords.length}
          deltaSummary={data.delta}
          cycleChangeKinds={Array.from(cycleChangeKinds)}
          showChanges={showChanges}
          setShowChanges={setShowChanges}
        />

        {/* Coverage banner */}
        <div className="pointer-events-none absolute left-3 top-3 z-[400] flex max-w-[calc(100%-1.5rem)] flex-col gap-2 sm:left-4 sm:top-4">
          <div className="pointer-events-auto flex items-center gap-2 rounded-full border border-ink-200 bg-white/90 px-3 py-1.5 text-xs shadow-soft backdrop-blur">
            <span className="inline-flex h-2 w-2 animate-pulse rounded-full bg-brand-500" />
            <span className="font-medium text-ink-700">
              {filteredWithCoords.length.toLocaleString("pt-BR")} pins no mapa
            </span>
            <span className="text-ink-400">·</span>
            <span className="text-ink-500">
              {filtered.length.toLocaleString("pt-BR")} após filtros
            </span>
          </div>
          {!legendDismissed && (
            <div className="pointer-events-auto flex items-center gap-3 rounded-full border border-ink-200 bg-white/90 px-3 py-1.5 text-xs shadow-soft backdrop-blur">
              <LegendDot color="#1f9270" label="Farmácia" />
              <LegendDot color="#f56d18" label="Cosméticos" />
              <button
                onClick={() => setLegendDismissed(true)}
                className="ml-2 text-ink-400 transition hover:text-ink-700"
                aria-label="Fechar legenda"
              >
                ×
              </button>
            </div>
          )}
        </div>

        {/* Mudanças do ciclo panel */}
        {showChanges && (
          <ChangesPanel
            rows={filteredDeltaRows}
            summary={data.delta}
            cycle={data.cycle}
            onClose={() => setShowChanges(false)}
            onJump={(r) => {
              const e = data.establishments.find((x) => x.cnpj_raw === r.cnpj_raw);
              if (e && e.lat != null && e.lon != null && mapRef.current) {
                mapRef.current.flyTo([e.lat, e.lon], 16, { duration: 0.9 });
                setSelected(e);
                setShowChanges(false);
              } else if (e) {
                setSelected(e);
                setShowChanges(false);
              }
            }}
          />
        )}

        {/* Establishment modal */}
        {selected && (
          <EstablishmentModal
            est={selected}
            delta={deltaByCnpj.get(selected.cnpj_raw) || null}
            onClose={() => setSelected(null)}
          />
        )}
      </div>
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block h-3 w-3 rounded-full ring-2 ring-white"
        style={{ background: color }}
      />
      <span className="text-ink-600">{label}</span>
    </span>
  );
}

function SidebarFilters({
  filters,
  setFilters,
  situacoes,
  municipalities,
  resetFilters,
  totalShown,
  totalWithCoords,
  deltaSummary,
  cycleChangeKinds,
  showChanges,
  setShowChanges,
  onFocusMunicipality
}: any) {
  return (
    <>
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-xs font-bold uppercase tracking-wider text-ink-500">Filtros</h2>
          <button
            onClick={resetFilters}
            className="text-xs font-semibold text-brand-700 transition hover:text-brand-900"
          >
            Limpar
          </button>
        </div>
        <input
          type="search"
          placeholder="Buscar razão social, CNPJ, bairro…"
          value={filters.search}
          onChange={(e) => setFilters({ ...filters, search: e.target.value })}
          className="w-full rounded-lg border border-ink-200 bg-ink-50 px-3 py-2 text-sm outline-none transition focus:border-brand-500 focus:bg-white focus:shadow-ring"
        />
        <Select
          label="Município"
          value={filters.municipio}
          onChange={(v) => setFilters({ ...filters, municipio: v })}
          options={[
            { value: "ALL", label: "Todos os municípios" },
            ...municipalities.map((m: any) => ({
              value: m.name,
              label: `${m.name} · ${m.total.toLocaleString("pt-BR")}`
            }))
          ]}
        />
        <Select
          label="Camada"
          value={filters.camada}
          onChange={(v) => setFilters({ ...filters, camada: v as any })}
          options={[
            { value: "ALL", label: "Farmácias + Cosméticos" },
            { value: "FARMACIA", label: "Só farmácias (4771-7)" },
            { value: "COSMETICOS", label: "Só cosméticos/perfumaria (4772-5)" }
          ]}
        />
        <Select
          label="Situação cadastral"
          value={filters.situacao}
          onChange={(v) => setFilters({ ...filters, situacao: v })}
          options={[
            { value: "ALL", label: "Todas as situações" },
            ...situacoes.map((s: string) => ({ value: s, label: s }))
          ]}
        />
        <Select
          label="Tipo"
          value={filters.tipo}
          onChange={(v) => setFilters({ ...filters, tipo: v as any })}
          options={[
            { value: "ALL", label: "Matriz + Filial" },
            { value: "MATRIZ", label: "Só matrizes" },
            { value: "FILIAL", label: "Só filiais" }
          ]}
        />
        <label className="flex items-center gap-2 rounded-lg bg-accent-50 px-3 py-2 text-xs font-semibold text-accent-800">
          <input
            type="checkbox"
            checked={filters.onlyChanges}
            onChange={(e) => setFilters({ ...filters, onlyChanges: e.target.checked })}
            className="h-4 w-4 rounded border-accent-300 text-accent-600 focus:ring-accent-500"
          />
          Mostrar só quem mudou neste ciclo
        </label>
      </section>

      <section className="rounded-xl border border-brand-100 bg-brand-50 p-3">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-bold uppercase tracking-wider text-brand-800">
            Mudanças do ciclo
          </h3>
          <span className="rounded-full bg-brand-500 px-2 py-0.5 text-[10px] font-bold text-white">
            {deltaSummary.total}
          </span>
        </div>
        <p className="mt-1 text-xs text-brand-900/80">
          Quem abriu, baixou ou mudou neste mês.
        </p>
        <button
          onClick={() => setShowChanges(!showChanges)}
          className="mt-2 w-full rounded-lg bg-brand-600 px-3 py-2 text-xs font-bold text-white shadow-soft transition hover:bg-brand-700 active:translate-y-px"
        >
          {showChanges ? "Ocultar painel" : "Abrir painel"}
        </button>
        <ul className="mt-3 space-y-1 text-xs text-brand-900">
          <CycleStat label="Novos" value={deltaSummary.novas} color="bg-emerald-500" />
          <CycleStat label="Baixados" value={deltaSummary.baixas} color="bg-rose-500" />
          <CycleStat
            label="Mudaram endereço"
            value={deltaSummary.mudaram_endereco}
            color="bg-amber-500"
          />
          <CycleStat
            label="Mudaram situação"
            value={deltaSummary.mudaram_situacao}
            color="bg-sky-500"
          />
        </ul>
      </section>

      <section>
        <h3 className="text-xs font-bold uppercase tracking-wider text-ink-500">Municípios</h3>
        <ul className="mt-2 space-y-1">
          {municipalities.map((m: any) => (
            <li key={m.name}>
              <button
                onClick={() => {
                  setFilters({ ...filters, municipio: m.name });
                  onFocusMunicipality(m.name);
                }}
                className={`flex w-full items-center justify-between rounded-lg px-3 py-1.5 text-left text-sm transition ${
                  filters.municipio === m.name
                    ? "bg-ink-900 text-white"
                    : "text-ink-700 hover:bg-ink-100"
                }`}
              >
                <span>{m.name}</span>
                <span className="tabular-nums text-xs opacity-80">
                  {m.farmacia.toLocaleString("pt-BR")}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section className="mt-auto rounded-xl bg-ink-100 p-3 text-xs text-ink-600">
        <strong className="text-ink-800">Cobertura do mapa</strong>
        <div className="mt-1 tabular-nums">
          {totalWithCoords.toLocaleString("pt-BR")} com coordenadas
        </div>
        <div className="opacity-70">de {totalShown.toLocaleString("pt-BR")} visíveis</div>
      </section>
    </>
  );
}

function CycleStat({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <li className="flex items-center justify-between">
      <span className="inline-flex items-center gap-2">
        <span className={`inline-block h-2 w-2 rounded-full ${color}`} />
        {label}
      </span>
      <span className="font-bold tabular-nums">{value.toLocaleString("pt-BR")}</span>
    </li>
  );
}

function Select({
  label,
  value,
  onChange,
  options
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="font-semibold uppercase tracking-wide text-ink-500">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-ink-200 bg-white px-2 py-2 text-sm text-ink-800 outline-none transition focus:border-brand-500 focus:shadow-ring"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function MobileBar(props: any) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="absolute bottom-4 left-1/2 z-[450] flex -translate-x-1/2 items-center gap-2 rounded-full border border-ink-200 bg-white px-4 py-2 text-xs font-bold text-ink-800 shadow-soft transition hover:bg-ink-50 lg:hidden"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <path d="M3 6h18M6 12h12M9 18h6" />
        </svg>
        Filtros e ciclo
      </button>
      {open && (
        <div
          className="anim-overlay fixed inset-0 z-[1000] flex items-end bg-black/40 lg:hidden"
          onClick={() => setOpen(false)}
        >
          <div
            className="anim-modal max-h-[85vh] w-full overflow-y-auto rounded-t-2xl bg-white p-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-base font-bold text-ink-900">Filtros</h3>
              <button
                onClick={() => setOpen(false)}
                className="rounded-full p-1.5 text-ink-500 hover:bg-ink-100"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </div>
            <SidebarFilters
              {...props}
              onFocusMunicipality={(n: string) => {
                props.onFocusMunicipality(n);
                setOpen(false);
              }}
            />
          </div>
        </div>
      )}
    </>
  );
}

function ChangesPanel({
  rows,
  summary,
  cycle,
  onClose,
  onJump
}: {
  rows: DeltaRow[];
  summary: { novas: number; baixas: number; mudaram_endereco: number; mudaram_situacao: number; total: number };
  cycle: string;
  onClose: () => void;
  onJump: (r: DeltaRow) => void;
}) {
  const [kind, setKind] = useState<string>("ALL");
  const visible = rows.filter((r) => kind === "ALL" || r.kind === kind);

  return (
    <div className="anim-slide absolute right-0 top-0 z-[500] flex h-full w-full max-w-md flex-col border-l border-ink-200 bg-white shadow-soft">
      <header className="flex items-center justify-between border-b border-ink-200 bg-gradient-to-br from-brand-50 to-white px-4 py-3">
        <div>
          <div className="text-[10px] font-bold uppercase tracking-wider text-brand-700">
            Ciclo {cycle}
          </div>
          <h2 className="text-base font-bold text-ink-900">Mudanças do ciclo</h2>
        </div>
        <button
          onClick={onClose}
          className="rounded-full p-1.5 text-ink-500 transition hover:bg-ink-100 hover:text-ink-800"
          aria-label="Fechar"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </header>
      <div className="flex flex-wrap gap-1 border-b border-ink-200 bg-ink-50 px-3 py-2 text-xs">
        <Chip active={kind === "ALL"} onClick={() => setKind("ALL")}>
          Todos <span className="ml-1 font-bold">({summary.total})</span>
        </Chip>
        <Chip active={kind === "NOVA"} onClick={() => setKind("NOVA")}>
          Novos <span className="ml-1 font-bold">({summary.novas})</span>
        </Chip>
        <Chip active={kind === "BAIXA"} onClick={() => setKind("BAIXA")}>
          Baixados <span className="ml-1 font-bold">({summary.baixas})</span>
        </Chip>
        <Chip active={kind === "MUDOU_ENDERECO"} onClick={() => setKind("MUDOU_ENDERECO")}>
          Endereço <span className="ml-1 font-bold">({summary.mudaram_endereco})</span>
        </Chip>
        <Chip active={kind === "MUDOU_SITUACAO"} onClick={() => setKind("MUDOU_SITUACAO")}>
          Situação <span className="ml-1 font-bold">({summary.mudaram_situacao})</span>
        </Chip>
      </div>
      <ul className="flex-1 divide-y divide-ink-100 overflow-y-auto">
        {visible.length === 0 && (
          <li className="p-6 text-center text-sm text-ink-500">
            Sem mudanças neste filtro.
          </li>
        )}
        {visible.map((r) => (
          <li key={`${r.cnpj_raw}-${r.kind}`}>
            <button
              onClick={() => onJump(r)}
              className="block w-full px-4 py-3 text-left transition hover:bg-ink-50"
            >
              <div className="flex items-center gap-2">
                <KindBadge kind={r.kind} />
                <span className="text-xs text-ink-500">{r.municipio_nome}</span>
              </div>
              <div className="mt-1 font-semibold text-ink-900">
                {r.nome_fantasia || r.razao_social || "—"}
              </div>
              <div className="mt-0.5 text-xs text-ink-500">
                {r.cnpj} · {r.razao_social}
              </div>
              {r.situacao && (
                <div className="mt-1 text-xs text-ink-600">
                  Situação atual: <span className="font-semibold">{r.situacao}</span>
                </div>
              )}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Chip({
  active,
  onClick,
  children
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-3 py-1 text-xs font-semibold transition ${
        active ? "bg-brand-600 text-white" : "bg-white text-ink-700 hover:bg-ink-100"
      }`}
    >
      {children}
    </button>
  );
}

function KindBadge({ kind }: { kind: string }) {
  const map: Record<string, { label: string; color: string }> = {
    NOVA: { label: "Novo", color: "bg-emerald-100 text-emerald-800 border-emerald-200" },
    BAIXA: { label: "Baixado", color: "bg-rose-100 text-rose-800 border-rose-200" },
    MUDOU_ENDERECO: {
      label: "Mudou endereço",
      color: "bg-amber-100 text-amber-800 border-amber-200"
    },
    MUDOU_SITUACAO: {
      label: "Mudou situação",
      color: "bg-sky-100 text-sky-800 border-sky-200"
    }
  };
  const m = map[kind] || { label: kind, color: "bg-ink-100 text-ink-700 border-ink-200" };
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${m.color}`}
    >
      {m.label}
    </span>
  );
}

function EstablishmentModal({
  est,
  delta,
  onClose
}: {
  est: Establishment;
  delta: DeltaRow | null;
  onClose: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="anim-overlay fixed inset-0 z-[1500] flex items-end justify-center bg-black/55 backdrop-blur-sm sm:items-center sm:justify-end"
      onClick={onClose}
    >
      <div
        className="anim-modal flex max-h-[92vh] w-full max-w-xl flex-col overflow-hidden rounded-t-2xl bg-white shadow-soft sm:mr-4 sm:mt-4 sm:h-[calc(100vh-2rem)] sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header
          className={`flex items-start justify-between gap-3 px-5 py-4 ${
            est.layer === "FARMACIA" ? "bg-brand-700 text-white" : "bg-accent-600 text-white"
          }`}
        >
          <div>
            <div className="text-[10px] font-bold uppercase tracking-widest opacity-80">
              {est.layer_label}
            </div>
            <h3 className="text-lg font-extrabold leading-snug">
              {est.nome_fantasia || est.razao_social || "Estabelecimento sem nome"}
            </h3>
            <div className="mt-0.5 text-xs opacity-90">{est.razao_social}</div>
          </div>
          <button
            onClick={onClose}
            className="rounded-full bg-white/15 p-1.5 transition hover:bg-white/25"
            aria-label="Fechar"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </header>
        {delta && (
          <div className="border-b border-ink-200 bg-amber-50 px-5 py-2 text-xs text-amber-900">
            <strong>Novidade deste ciclo:</strong>{" "}
            <KindBadge kind={delta.kind} />{" "}
            <span className="opacity-80">{delta.detected_at.slice(0, 10)}</span>
          </div>
        )}
        <div className="flex-1 overflow-y-auto px-5 py-4 text-sm text-ink-800">
          <Section title="Identificação">
            <Field label="CNPJ" value={est.cnpj} mono />
            <Field label="Tipo" value={est.matriz_filial} />
            <Field label="Razão social" value={est.razao_social} />
            <Field label="Nome fantasia" value={est.nome_fantasia || "—"} />
            <Field label="CNAE principal" value={est.cnae_principal} />
            {est.cnae_secundaria && (
              <Field label="CNAEs secundárias" value={est.cnae_secundaria} />
            )}
          </Section>
          <Section title="Endereço">
            <Field
              label="Logradouro"
              value={`${est.logradouro_full} ${est.numero || ""}${est.complemento ? ` — ${est.complemento}` : ""}`}
            />
            <Field label="Bairro" value={est.bairro} />
            <Field label="CEP" value={est.cep} mono />
            <Field label="Município" value={`${est.municipio_nome} / ${est.uf}`} />
          </Section>
          <Section title="Contato">
            <Field label="Telefone 1" value={est.telefone1} />
            <Field label="Telefone 2" value={est.telefone2 || "—"} />
            <Field label="E-mail" value={est.email || "—"} mono />
          </Section>
          <Section title="Situação e porte">
            <Field
              label="Situação cadastral"
              value={
                <span
                  className={`inline-flex rounded-full px-2 py-0.5 text-xs font-bold ${
                    est.situacao === "ATIVA"
                      ? "bg-emerald-100 text-emerald-800"
                      : est.situacao === "BAIXADA"
                      ? "bg-rose-100 text-rose-800"
                      : est.situacao === "SUSPENSA"
                      ? "bg-amber-100 text-amber-800"
                      : est.situacao === "INAPTA"
                      ? "bg-orange-100 text-orange-800"
                      : "bg-ink-100 text-ink-700"
                  }`}
                >
                  {est.situacao || "—"}
                </span>
              }
            />
            <Field label="Data da situação" value={est.situacao_data} mono />
            <Field label="Motivo" value={est.motivo_situacao || "—"} />
            <Field label="Data de abertura" value={est.abertura_data} mono />
            <Field label="Porte" value={est.porte || "—"} />
            <Field
              label="Capital social"
              value={
                est.capital_social != null
                  ? est.capital_social.toLocaleString("pt-BR", {
                      style: "currency",
                      currency: "BRL"
                    })
                  : "—"
              }
            />
            <Field label="Simples" value={est.simples || "—"} />
            <Field label="MEI" value={est.mei || "—"} />
          </Section>
        </div>
        <footer className="border-t border-ink-200 bg-ink-50 px-5 py-3 text-[11px] text-ink-500">
          Fonte: Receita Federal — base CNPJ aberta, ciclo {est._key ? "vigente" : ""}.
        </footer>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-5 last:mb-0">
      <h4 className="mb-2 text-[10px] font-bold uppercase tracking-widest text-ink-500">
        {title}
      </h4>
      <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">{children}</dl>
    </section>
  );
}

function Field({
  label,
  value,
  mono
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-[10px] font-semibold uppercase tracking-wider text-ink-500">
        {label}
      </dt>
      <dd className={`text-sm text-ink-800 ${mono ? "font-mono tabular-nums" : ""}`}>
        {value || "—"}
      </dd>
    </div>
  );
}

function escapeHtml(s: string) {
  return (s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}