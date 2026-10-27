"""
Nucleo reutilizavel do extrator de logradouros de mapa PDF municipal.

CLI:
    python extrair_logradouros.py mapa.pdf municipio.zip \
        --municipio Cajazeiras --uf PB --out ./output
"""
from __future__ import annotations

import json
import math
import re
import tempfile
import unicodedata
import zipfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

PREFIXOS_RE = re.compile(
    r"^(RUA|AVENIDA|AV\.?|TRAVESSA|TRAV\.?|ALAMEDA|AL\.?|ESTRADA|RODOVIA|"
    r"LARGO|PRA[CÇ]A|PCA\.?|BECO|VIELA|PASSAGEM)\b",
    re.IGNORECASE,
)

ANOTACAO_RE = re.compile(
    r"\(\s*(?:EM\s+VOTA[CÇ][AÃ]O|COM\s+AUT[OÓ]GRAFO)\s*\)",
    re.IGNORECASE,
)

TIPOS_LOGRADOURO = {
    "RUA", "R", "AVENIDA", "AV", "TRAVESSA", "TRAV", "ALAMEDA", "AL",
    "ESTRADA", "RODOVIA", "LARGO", "PRACA", "PCA", "BECO", "VIELA", "PASSAGEM",
}

LAYER_NAMES = [
    "1_match_shapefile",
    "2_match_osm",
    "3_match_fuzzy",
    "4_georreferenciados",
    "5_rejeitados_auditoria",
]


def _normalizar(s) -> str:
    if not isinstance(s, str) or not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


def _strip_prefixo(norm: str) -> str:
    toks = norm.split()
    if len(toks) <= 1:
        return "" if toks and toks[0] in TIPOS_LOGRADOURO else norm
    if toks[0] in TIPOS_LOGRADOURO:
        return " ".join(toks[1:])
    return norm


def _remover_anotacoes(s: str) -> str:
    return ANOTACAO_RE.sub("", str(s)).strip()


def _detectar_coluna_nome(gdf: gpd.GeoDataFrame) -> str | None:
    candidatos = ["nome", "name", "logradouro", "rua", "nm_rua", "ds_nome"]
    lower_map = {c.lower(): c for c in gdf.columns}
    for cand in candidatos:
        if cand in lower_map:
            col = lower_map[cand]
            if gdf[col].notna().mean() >= 0.5:
                return col
    for c in gdf.columns:
        if c == "geometry":
            continue
        if gdf[c].dtype == object and gdf[c].notna().mean() >= 0.5:
            sample = gdf[c].dropna().astype(str).head(20)
            if len(sample) and sample.str.len().mean() > 2:
                return c
    return None


def ler_shapefile(path: str, crs: str, log: list[str]) -> gpd.GeoDataFrame | None:
    p = Path(path)
    if p.suffix.lower() == ".zip":
        tmpdir = Path(tempfile.mkdtemp())
        with zipfile.ZipFile(p) as z:
            z.extractall(tmpdir)
        cands = (list(tmpdir.rglob("*.shp"))
                 + list(tmpdir.rglob("*.gpkg"))
                 + list(tmpdir.rglob("*.geojson")))
        if not cands:
            raise ValueError("ZIP nao contem .shp/.gpkg/.geojson")
        p = cands[0]
        log.append(f"  Shapefile extraido do ZIP: {p.name}")

    gdf = None
    for kwargs in (
        {"engine": "pyogrio"},
        {"engine": "fiona"},
        {"engine": "fiona", "encoding": "latin-1"},
    ):
        try:
            gdf = gpd.read_file(p, **kwargs)
            log.append(f"  Lido com {kwargs}")
            break
        except Exception:
            gdf = None
    if gdf is None:
        raise RuntimeError(f"Falha ao ler {p} com qualquer engine/encoding")

    col_nome = _detectar_coluna_nome(gdf)
    if col_nome is None:
        log.append("AVISO: coluna de nome nao detectada no shapefile - usando apenas OSM")
        return None

    log.append(f"  Coluna de nome detectada: '{col_nome}'")
    gdf = gdf.to_crs(crs)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
    if gdf.empty:
        log.append("AVISO: shapefile sem feicoes LineString validas")
        return None

    gdf["nome_orig"] = gdf[col_nome].astype(str)
    gdf["nome_norm"] = gdf["nome_orig"].apply(_normalizar)
    gdf["nome_norm_np"] = gdf["nome_norm"].apply(_strip_prefixo)
    if "length" in gdf.columns:
        gdf["_comp"] = pd.to_numeric(gdf["length"], errors="coerce").fillna(0.0)
    else:
        gdf["_comp"] = gdf.geometry.length
    return gdf


def extrair_pdf(pdf_path: str) -> pd.DataFrame:
    import pymupdf
    doc = pymupdf.open(pdf_path)
    rows = []
    for page_num, page in enumerate(doc, start=1):
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                texto = "".join(s.get("text", "") for s in spans).strip()
                if not texto:
                    continue
                bbox = line.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                x0, y0, x1, y1 = bbox
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                dx, dy = line.get("dir", (1.0, 0.0))
                rows.append({
                    "page": page_num, "texto": texto,
                    "cx": cx, "cy": cy, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "dir_x": dx, "dir_y": dy,
                    "fonte": spans[0].get("font", ""),
                    "tamanho": spans[0].get("size", 0.0),
                })
    return pd.DataFrame(rows)


def filtrar_normalizar(df_raw: pd.DataFrame) -> pd.DataFrame:
    df = df_raw.copy()
    df = df[df["texto"].str.match(PREFIXOS_RE)].copy()
    df["texto_limpo"] = df["texto"].apply(_remover_anotacoes)
    df["nome_norm"] = df["texto_limpo"].apply(_normalizar)
    df["nome_norm_np"] = df["nome_norm"].apply(_strip_prefixo)
    df = df[df["nome_norm_np"].str.len() > 0].copy()
    df["_len"] = df["texto_limpo"].str.len()
    df = (df.sort_values("_len", ascending=False)
            .drop_duplicates("nome_norm")
            .drop(columns="_len")
            .reset_index(drop=True))
    return df


def baixar_osm(municipio: str, uf: str, crs: str, log: list[str]) -> gpd.GeoDataFrame | None:
    return None
    try:
        import osmnx as ox
    except ImportError:
        log.append("AVISO: osmnx nao instalado - seguindo apenas com shapefile")
        return None
    try:
        ox.settings.overpass_url = "https://overpass.private.coffee/api"
        ox.settings.requests_timeout = 300
        ox.settings.use_cache = True
    except Exception:
        pass
    try:
        G = ox.graph_from_place(f"{municipio}, {uf}, Brasil", network_type="drive")
        edges = ox.graph_to_gdfs(G, nodes=False)
    except Exception as e:
        log.append(f"AVISO: OSM indisponivel ({type(e).__name__}: {e}); seguindo apenas com shapefile")
        return None
    if "name" not in edges.columns:
        log.append("AVISO: OSM sem coluna 'name' - descartado")
        return None
    edges = edges[edges["name"].notna()].copy()

    def _first(x):
        if isinstance(x, (list, tuple)):
            return x[0] if len(x) else ""
        return x

    edges["nome_orig"] = edges["name"].apply(lambda x: str(_first(x)))
    edges["nome_norm"] = edges["nome_orig"].apply(_normalizar)
    edges["nome_norm_np"] = edges["nome_norm"].apply(_strip_prefixo)
    for c in edges.columns:
        if c != "geometry":
            edges[c] = edges[c].astype(str)
    edges = edges.to_crs(crs)
    edges["_comp"] = edges.geometry.length
    log.append(f"  OSM: {len(edges)} segmentos com nome")
    return edges


def build_index(gdf: gpd.GeoDataFrame | None, key_col: str = "nome_norm") -> dict:
    if gdf is None or gdf.empty:
        return {}
    idx: dict[str, dict] = {}
    for _, row in gdf.iterrows():
        key = row[key_col]
        if not key:
            continue
        comp = float(row.get("_comp", 0.0) or 0.0)
        if key not in idx or comp > idx[key]["comp"]:
            idx[key] = {"geom": row.geometry, "nome": row["nome_orig"], "comp": comp}
    return idx


def match_exato(df_pdf, idx_full, idx_nopref, fonte):
    out, used = [], set()
    for i, row in df_pdf.iterrows():
        hit = idx_full.get(row["nome_norm"]) or idx_nopref.get(row["nome_norm_np"])
        if hit is None:
            continue
        out.append({
            "pdf_idx": i, "logradouro": row["texto_limpo"], "nome_norm": row["nome_norm"],
            "cx": float(row["cx"]), "cy": float(row["cy"]),
            "fonte": fonte, "nome_fonte": hit["nome"], "score": 1.0,
            "geometry": hit["geom"],
        })
        used.add(i)
    return out, used


def match_fuzzy(df_pdf, sources, cutoff=0.80):
    out = []
    for _, row in df_pdf.iterrows():
        best = None
        for fonte, prioridade, idx in sources:
            for k, hit in idx.items():
                s = SequenceMatcher(None, row["nome_norm"], k).ratio()
                if s < cutoff:
                    continue
                cand = (s, -prioridade)
                if best is None or cand > best[0]:
                    best = (cand, fonte, hit, k, s)
        if best:
            _, fonte, hit, k, s = best
            out.append({
                "pdf_idx": row.name, "logradouro": row["texto_limpo"], "nome_norm": k,
                "cx": float(row["cx"]), "cy": float(row["cy"]),
                "fonte": fonte, "nome_fonte": hit["nome"], "score": float(s),
                "geometry": hit["geom"],
            })
    return out


def _make_ransac():
    from sklearn.linear_model import LinearRegression, RANSACRegressor
    try:
        return RANSACRegressor(estimator=LinearRegression(), random_state=42, min_samples=3)
    except TypeError:
        return RANSACRegressor(base_estimator=LinearRegression(), random_state=42, min_samples=3)


def ancoras_de(matches):
    out = []
    for m in matches:
        g = m.get("geometry")
        if g is None or g.is_empty:
            continue
        c = g.centroid
        out.append({"cx": m["cx"], "cy": m["cy"], "x_geo": float(c.x), "y_geo": float(c.y)})
    return out


def calibrar_afim(ancoras, log):
    if len(ancoras) < 3:
        log.append(f"AVISO: apenas {len(ancoras)} ancoras - modelo afim nao calibrável")
        return None, {"erro_mediano_m": None, "erro_p95_m": None,
                      "n_ancoras": len(ancoras), "coef_x": None, "coef_y": None}
    X = np.array([[a["cx"], a["cy"]] for a in ancoras], dtype=float)
    Yx = np.array([a["x_geo"] for a in ancoras], dtype=float)
    Yy = np.array([a["y_geo"] for a in ancoras], dtype=float)
    mx = _make_ransac().fit(X, Yx)
    my = _make_ransac().fit(X, Yy)
    errs = np.hypot(mx.predict(X) - Yx, my.predict(X) - Yy)
    em, p95 = float(np.median(errs)), float(np.percentile(errs, 95))
    log.append(f"  Modelo afim: {len(ancoras)} ancoras | erro mediano={em:.1f} m | P95={p95:.1f} m")
    if em > 500:
        log.append("AVISO: erro mediano > 500 m - modelo impreciso; auditar saidas!")
    meta = {
        "erro_mediano_m": em, "erro_p95_m": p95, "n_ancoras": len(ancoras),
        "coef_x": {"a": float(mx.estimator_.coef_[0]),
                   "b": float(mx.estimator_.coef_[1]),
                   "c": float(mx.estimator_.intercept_)},
        "coef_y": {"a": float(my.estimator_.coef_[0]),
                   "b": float(my.estimator_.coef_[1]),
                   "c": float(my.estimator_.intercept_)},
    }
    return (mx, my), meta


def aplicar_afim(modelo, cx, cy):
    mx, my = modelo
    X = np.array([[cx, cy]], dtype=float)
    return float(mx.predict(X)[0]), float(my.predict(X)[0])


def classificar_confianca(fonte, score, dist, erro):
    if erro is not None and erro > 500:
        return "rejeitar"
    if score == 1.0 and fonte in ("shapefile", "osm"):
        if dist is None:
            return "baixa"
        if dist < 100:
            return "alta"
        if dist > 2000:
            return "rejeitar"
        return "baixa"
    if dist is None:
        return "baixa"
    if dist > 2000:
        return "rejeitar"
    if score is not None and score >= 0.85 and dist < 500:
        return "média"
    return "baixa"


def classificar_matches(matches, modelo, erro):
    for m in matches:
        if modelo is not None:
            px, py = aplicar_afim(modelo, m["cx"], m["cy"])
        else:
            px = py = None
        if m["geometry"] is not None and px is not None:
            c = m["geometry"].centroid
            m["dist_ancora_m"] = math.hypot(c.x - px, c.y - py)
        else:
            m["dist_ancora_m"] = None
        m["confianca"] = classificar_confianca(m["fonte"], m["score"], m["dist_ancora_m"], erro)
    return matches


def projetar_ausentes(df_pdf_restante, modelo, erro, crs):
    out = []
    for _, row in df_pdf_restante.iterrows():
        if modelo is not None:
            x, y = aplicar_afim(modelo, float(row["cx"]), float(row["cy"]))
            geom = Point(x, y)
            conf = "ausente_georreferenciado" if (erro or 0) <= 500 else "rejeitar"
        else:
            geom, conf = None, "rejeitar"
        out.append({
            "logradouro": row["texto_limpo"], "nome_fonte": "", "fonte": "modelo_afim",
            "origem": "pdf_sem_match", "confianca": conf,
            "dist_ancora_m": None, "score": None, "geometry": geom,
        })
    return out


def deduplicar(gdf):
    if len(gdf) <= 1:
        return gdf
    gdf = gdf.copy()
    gdf["_h"] = gdf["geometry"].apply(
        lambda g: g.wkb_hex if g is not None and not g.is_empty else f"_e_{id(g)}"
    )
    keep = []
    for _, grp in gdf.groupby("_h", sort=False):
        rows = list(grp.iterrows())
        used = set()
        for i, (idx, row) in enumerate(rows):
            if idx in used:
                continue
            used.add(idx)
            keep.append(idx)
            for idx2, row2 in rows[i + 1:]:
                if idx2 in used:
                    continue
                r = SequenceMatcher(
                    None,
                    _normalizar(str(row["logradouro"])),
                    _normalizar(str(row2["logradouro"])),
                ).ratio()
                if r >= 0.75:
                    used.add(idx2)
    return gdf.loc[keep].drop(columns="_h")


def construir_camadas(matches, ausentes_proj, data_str):
    layers = {n: [] for n in LAYER_NAMES}
    for m in matches:
        if m["fonte"] == "shapefile":
            origem = "pdf_match_exato_shp"
        elif m["score"] == 1.0:
            origem = "pdf_match_exato_osm"
        else:
            origem = "pdf_match_fuzzy"
        rec = {
            "logradouro": m["logradouro"], "nome_fonte": m.get("nome_fonte", ""),
            "fonte": m["fonte"], "origem": origem, "confianca": m["confianca"],
            "data": data_str, "dist_ancora_m": m.get("dist_ancora_m"),
            "score": m.get("score"), "geometry": m["geometry"],
        }
        if m["confianca"] == "rejeitar":
            layers["5_rejeitados_auditoria"].append(rec)
        elif m["score"] is not None and m["score"] < 1.0:
            layers["3_match_fuzzy"].append(rec)
        elif m["fonte"] == "shapefile":
            layers["1_match_shapefile"].append(rec)
        elif m["fonte"] == "osm":
            layers["2_match_osm"].append(rec)
    for a in ausentes_proj:
        rec = {**a, "data": data_str}
        if a["confianca"] == "rejeitar":
            layers["5_rejeitados_auditoria"].append(rec)
        else:
            layers["4_georreferenciados"].append(rec)
    return layers


def escrever_gpkg(out_dir, layers, crs, log):
    gpkg = out_dir / "logradouros_FINAL.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    cols = ["logradouro", "nome_fonte", "fonte", "origem",
            "confianca", "data", "dist_ancora_m", "score"]
    mode = "w"
    for name in LAYER_NAMES:
        feats = layers.get(name, [])
        try:
            if feats:
                gdf = gpd.GeoDataFrame(feats, geometry="geometry", crs=crs)
                gdf = deduplicar(gdf)
                for c in cols:
                    if c not in gdf.columns:
                        gdf[c] = None
                gdf = gdf[cols + ["geometry"]]
            else:
                gdf = gpd.GeoDataFrame({c: [] for c in cols}, geometry=[], crs=crs)
            gdf.to_file(gpkg, layer=name, driver="GPKG", mode=mode)
            mode = "a"
            log.append(f"  Camada '{name}': {len(gdf)} feicoes")
        except Exception as e:
            log.append(f"AVISO: falha ao gravar camada '{name}': {e}")
    return gpkg


def escrever_csv(out_dir, layers):
    rows = []
    for camada, feats in layers.items():
        for f in feats:
            g = f.get("geometry")
            if g is not None and not g.is_empty:
                c = g.centroid if g.geom_type in ("LineString", "MultiLineString") else g
                x, y = float(c.x), float(c.y)
            else:
                x = y = None
            rows.append({
                "camada": camada, "logradouro": f.get("logradouro"),
                "nome_fonte": f.get("nome_fonte"), "fonte": f.get("fonte"),
                "origem": f.get("origem"), "confianca": f.get("confianca"),
                "data": f.get("data"), "dist_ancora_m": f.get("dist_ancora_m"),
                "score": f.get("score"), "x": x, "y": y,
            })
    p = out_dir / "logradouros.csv"
    pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
    return p


def escrever_relatorio(out_dir, layers, meta, log, municipio, uf):
    cobertura = {k: len(v) for k, v in layers.items()}
    linhas = [
        "RELATORIO DE EXTRACAO E GEORREFERENCIAMENTO",
        f"Municipio: {municipio}/{uf}",
        f"Data: {datetime.now().isoformat(timespec='seconds')}",
        "", "--- COBERTURA POR CAMADA ---",
    ]
    for k, v in cobertura.items():
        linhas.append(f"  {k}: {v}")
    linhas += [
        "", "--- MODELO AFIM (RANSAC) ---",
        f"  Ancoras: {meta.get('n_ancoras')}",
        f"  Erro mediano: {meta.get('erro_mediano_m')} m",
        f"  Erro P95: {meta.get('erro_p95_m')} m",
        "", "--- LOG ---",
    ]
    linhas += log
    p = out_dir / "relatorio.txt"
    p.write_text("\n".join(str(x) for x in linhas), encoding="utf-8")
    return p


def empacotar(out_dir, municipio):
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", municipio).strip("_").lower() or "municipio"
    zp = out_dir / f"logradouros_{slug}.zip"
    if zp.exists():
        zp.unlink()
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in out_dir.iterdir():
            if f.name == zp.name:
                continue
            if f.suffix.lower() in (".gpkg", ".csv", ".json") or f.name == "relatorio.txt":
                z.write(f, f.name)
    return zp


def processar_pipeline(pdf_path, shp_path, municipio, uf, out_dir, crs="EPSG:31984"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    data_str = datetime.now().strftime("%Y-%m-%d")

    log.append("[ETAPA 0] Lendo shapefile do municipio")
    gdf_shp = ler_shapefile(shp_path, crs, log)

    log.append("[ETAPA 1] Extraindo texto do PDF")
    df_raw = extrair_pdf(pdf_path)
    log.append(f"  {len(df_raw)} linhas extraidas do PDF")

    log.append("[ETAPA 2] Filtrando e normalizando")
    df_pdf = filtrar_normalizar(df_raw)
    log.append(f"  {len(df_pdf)} logradouros unicos apos filtro")
    if df_pdf.empty:
        raise RuntimeError("Nenhum logradouro encontrado no PDF apos filtro")

    log.append("[ETAPA 3] Tentando baixar OSM (fallback gracioso)")
    gdf_osm = baixar_osm(municipio, uf, crs, log)

    log.append("[ETAPA 4] Matching em tres fontes")
    idx_shp = build_index(gdf_shp, "nome_norm")
    idx_shp_np = build_index(gdf_shp, "nome_norm_np")
    idx_osm = build_index(gdf_osm, "nome_norm")
    idx_osm_np = build_index(gdf_osm, "nome_norm_np")

    m_shp, used_shp = match_exato(df_pdf, idx_shp, idx_shp_np, "shapefile")
    log.append(f"  match exato shapefile: {len(m_shp)}")

    restante1 = df_pdf.drop(index=list(used_shp))
    m_osm, used_osm = match_exato(restante1, idx_osm, idx_osm_np, "osm")
    log.append(f"  match exato OSM: {len(m_osm)}")

    restante2 = restante1.drop(index=list(used_osm))
    sources = []
    if idx_shp:
        sources.append(("shapefile", 0, idx_shp))
    if idx_osm:
        sources.append(("osm", 1, idx_osm))
    m_fuzzy = match_fuzzy(restante2, sources) if sources else []
    log.append(f"  match fuzzy: {len(m_fuzzy)}")

    used_fuzzy = {m["pdf_idx"] for m in m_fuzzy}
    restante3 = restante2.drop(index=list(used_fuzzy)) if used_fuzzy else restante2
    log.append(f"  sem match (georreferenciar pelo modelo afim): {len(restante3)}")

    log.append("[ETAPA 5] Calibrando modelo afim")
    ancoras = ancoras_de(m_shp + m_osm)
    modelo, meta = calibrar_afim(ancoras, log)
    (out / "modelo_afim.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    todos = classificar_matches(m_shp + m_osm + m_fuzzy, modelo, meta.get("erro_mediano_m"))
    ausentes = projetar_ausentes(restante3, modelo, meta.get("erro_mediano_m"), crs)

    log.append("[ETAPA 6] Consolidando camadas")
    layers = construir_camadas(todos, ausentes, data_str)
    escrever_gpkg(out, layers, crs, log)
    escrever_csv(out, layers)
    escrever_relatorio(out, layers, meta, log, municipio, uf)

    log.append("[ETAPA 7] Empacotando ZIP")
    return str(empacotar(out, municipio))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("shp")
    ap.add_argument("--municipio", required=True)
    ap.add_argument("--uf", required=True)
    ap.add_argument("--out", default="./output")
    ap.add_argument("--crs", default="EPSG:31984")
    a = ap.parse_args()
    print("OK:", processar_pipeline(a.pdf, a.shp, a.municipio, a.uf, a.out, a.crs))
