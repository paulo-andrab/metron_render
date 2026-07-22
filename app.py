import io
import csv
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request

app = Flask(__name__)


# ---------------------------------------------------------------------------
#  XML PARSER — Extrai campos-folha recursivamente com herança de occurs
# ---------------------------------------------------------------------------
def parse_xml_structure(element, fields_list=None, inherited_occurs="1", seen_names=None):
    """
    Percorre a árvore XML recursivamente, extraindo campos-folha (fields).

    - Ignora containers (tags com filhos do tipo field/occurs/structure).
    - Ignora índices (.idx) e tags sem nome/tipo/tamanho.
    - Herda o valor de 'occurs' do bloco pai para os filhos.
    - Evita duplicatas via set 'seen_names'.

    Returns:
        list[dict]: lista de dicionários com os atributos de cada campo.
    """
    if fields_list is None:
        fields_list = []
    if seen_names is None:
        seen_names = set()

    raw_name = element.get("name", "").strip()
    raw_type = element.get("type", "").strip()

    # Herança de occurs
    current_occurs = inherited_occurs
    if element.tag.lower() == "occurs":
        occ_val = element.get("number") or element.get("maxOccurs") or element.get("occurs")
        if occ_val and occ_val.isdigit():
            current_occurs = occ_val
    else:
        self_occurs = element.get("occurs") or element.get("maxOccurs")
        if self_occurs and self_occurs.isdigit():
            current_occurs = self_occurs

    # Detecta containers (tags que agrupam outros campos)
    is_container = any(
        child.tag.lower() in ("field", "occurs", "structure") for child in element
    )

    # Filtragem — decide se o campo deve ser adicionado
    should_add = bool(raw_name)
    if should_add and (raw_name.lower().endswith(".idx") or raw_type.lower() == "idx"):
        should_add = False
    if should_add and is_container:
        should_add = False
    if should_add and (not raw_type and not element.get("size")):
        should_add = False
    if should_add and raw_name in seen_names:
        should_add = False

    if should_add:
        seen_names.add(raw_name)
        fields_list.append({
            "name":   raw_name,
            "type":   raw_type or "-",
            "size":   element.get("size",   "").strip() or "-",
            "scale":  element.get("scale",  "").strip() or "-",
            "digits": element.get("digit",  "").strip() or "-",
            "offset": element.get("offset", "").strip() or "-",
            "hidden": element.get("hidden", "").strip() or "-",
            "dbtype": element.get("dbtype", "").strip() or "-",
            "occurs": current_occurs,
        })

    for child in element:
        parse_xml_structure(child, fields_list, inherited_occurs=current_occurs, seen_names=seen_names)

    return fields_list


# ---------------------------------------------------------------------------
#  VALIDADORES DE TIPO — um por tipo XML
# ---------------------------------------------------------------------------
def _validate_date(csv_type_upper):
    if "TIMESTAMP" not in csv_type_upper and "DATE" not in csv_type_upper:
        return "TYPE MISMATCH: expected TIMESTAMP/DATE", "error"
    return None, None


def _validate_alphanum(csv_type_upper, xml_size, csv_prec):
    """
    Retorna (status, class, highlights).
    Acumula TYPE e PRECISION como erros independentes.
    """
    errors     = []
    highlights = set()

    if "CHAR" not in csv_type_upper:
        errors.append("TYPE MISMATCH: expected CHAR/VARCHAR")
        highlights.update({"xml_type", "csv_type"})

    if str(xml_size).strip() != str(csv_prec).strip():
        errors.append("PRECISION MISMATCH")
        highlights.update({"xml_size", "csv_prec"})

    if errors:
        return " | ".join(errors), "error", highlights
    return None, None, set()


def _validate_numunsigned(xf, csv_type_upper, csv_prec, csv_scale):
    """
    Retorna (status, class, highlights).
    Para DECIMAL acumula todos os erros — type, precision e scale são independentes.
    SMALLINT/INTEGER não validam precision — padrão COBOL.
    """
    try:
        size_val = int(xf["size"])
    except (ValueError, TypeError):
        size_val = 0

    has_scale = xf["scale"] not in ("", "-", "0")

    if has_scale or size_val >= 10:
        # --- DECIMAL: acumula todos os erros ---
        errors     = []
        highlights = set()

        if "DECIMAL" not in csv_type_upper and "NUMERIC" not in csv_type_upper:
            errors.append("TYPE MISMATCH: expected DECIMAL")
            highlights.update({"xml_type", "csv_type"})

        if str(xf["size"]).strip() != str(csv_prec).strip():
            errors.append("PRECISION MISMATCH")
            highlights.update({"xml_size", "csv_prec"})

        xml_s = str(xf["scale"]).strip()
        csv_s = str(csv_scale).strip()
        xml_s = "0" if xml_s in ("-", "", "0") else xml_s
        csv_s = "0" if csv_s in ("-", "", "0") else csv_s
        if has_scale and xml_s != csv_s:
            errors.append("SCALE MISMATCH")
            highlights.update({"xml_scale", "csv_scale"})

        if errors:
            return " | ".join(errors), "error", highlights
        return None, None, set()

    # --- SMALLINT / INTEGER (sem validação de precision) ---
    if 1 <= size_val <= 4:
        if "SMALLINT" not in csv_type_upper:
            return "TYPE MISMATCH: expected SMALLINT", "error", {"xml_type", "xml_size", "csv_type"}
    elif 5 <= size_val <= 9:
        if "INTEGER" not in csv_type_upper:
            return "TYPE MISMATCH: expected INTEGER", "error", {"xml_type", "xml_size", "csv_type"}

    return None, None, set()


def _validate_field(xf, csv_type_upper, csv_prec, csv_scale):
    """
    Despacha a validação para o handler correto.
    Retorna (status, class, highlights).
    highlights é um set com as células a marcar em laranja.
    """
    xml_dbtype = xf["dbtype"].lower()
    xml_type   = xf["type"].lower()

    if xml_dbtype == "date":
        status, cls = _validate_date(csv_type_upper)
        return status, cls, set()
    if xml_type == "alphanum":
        return _validate_alphanum(csv_type_upper, xf["size"], csv_prec)
    if xml_type == "numunsigned":
        return _validate_numunsigned(xf, csv_type_upper, csv_prec, csv_scale)

    return None, None, set()


# ---------------------------------------------------------------------------
#  COMPARAÇÃO XML × CSV
# ---------------------------------------------------------------------------
def _safe_int(value, default=0):
    """Converte string para int com fallback seguro."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _extract_xml_version(xml_content):
    """Extrai a versão de comentários no XML, ex: <!-- VERSAO 1.0 -->."""
    match = re.search(r'<!--\s*(?:VERS[AÃaã]O|VERSION)\s+(.*?)\s*-->', xml_content, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return "Desconhecida"


def _is_filler(xf):
    """
    Detecta campos filler pelo atributo type do XML.
    Filler COBOL é identificado pelo tipo 'filler' ou pelo padrão F99
    explicitamente definido como tipo de filler no layout.
    """
    xml_type = xf["type"].lower()
    return xml_type in ("filler", "f99", "fill")


def _compare_fields(xml_fields, csv_fields, csv_table="TABLE", schema="SCHEMA"):
    """
    Compara os campos XML contra o dicionário CSV.

    Returns:
        tuple: (rows, ok_count, err_count, missing_count, ignored_count,
                matched_occurs_bases)
    """
    rows = []
    ok_count = err_count = missing_count = ignored_count = 0
    xml_counter = 0
    matched_occurs_bases = set()
    prev_xml_name = None

    for xf in xml_fields:
        name_key   = xf["name"].lower()
        is_hidden  = xf["hidden"].strip().lower() in ("1", "true", "yes")
        xml_occurs = _safe_int(xf["occurs"], 1)

        # ID lógico
        if is_hidden:
            id_value = "HID"
        else:
            xml_counter += 1
            id_value = str(xml_counter)

        # Busca no CSV (direta ou via occurs _1)
        csv_info       = csv_fields.get(name_key)
        is_occurs_match = False

        if not csv_info and xml_occurs > 1:
            csv_info_occurs = csv_fields.get(f"{name_key}_1")
            if csv_info_occurs:
                csv_info        = csv_info_occurs
                is_occurs_match = True
                matched_occurs_bases.add(name_key)

        row_data = {
            "id":         id_value,
            "xml_name":   xf["name"],
            "xml_type":   xf["type"],
            "xml_size":   xf["size"],
            "xml_scale":  xf["scale"],
            "xml_digits": xf["digits"],
            "xml_offset": xf["offset"],
            "xml_hidden": "Sim" if is_hidden else "-",
            "csv_order":  "-",
            "csv_name":   "-",
            "csv_type":   "-",
            "csv_prec":   "-",
            "csv_scale":  "-",
            "status":     "",
            "class":      "",
        }

        if not csv_info:
            # Filler detectado pelo type do XML, não pelo nome
            if _is_filler(xf):
                row_data["status"]     = "IGNORED: filler"
                row_data["class"]      = "ignored"
                size = xf["size"]
                field = xf["name"]
                row_data["fix_query"]  = (
                    f"DISABLE INDEX ALL FOR {schema}.{csv_table};\n"
                    f"ALTER TABLE {schema}.{csv_table} MODIFY {field} TYPE TO CHAR({size});\n"
                    f"REBUILD INDEX ALL FOR {schema}.{csv_table};\n"
                    f"UPDATE STATISTICS {schema}.{csv_table};"
                )
                row_data["highlights"] = set()
                ignored_count += 1
            elif is_hidden:
                row_data["status"]     = "IGNORED: hidden"
                row_data["class"]      = "ignored"
                row_data["fix_query"]  = ""
                row_data["highlights"] = set()
                ignored_count += 1
            else:
                row_data["status"]     = "MISSING: field not in CSV"
                row_data["class"]      = "missing"
                
                xml_type = xf["type"].lower()
                xml_dbtype = xf["dbtype"].lower()
                size = xf["size"]
                scale = xf["scale"] if xf["scale"] not in ("-", "") else "0"
                sql_datatype = f"CHAR({size})"
                if xml_dbtype == "date":
                    sql_datatype = "TIMESTAMP"
                elif xml_type == "alphanum":
                    sql_datatype = f"CHAR({size})"
                elif xml_type == "numunsigned":
                    try:
                        size_val = int(size)
                    except (ValueError, TypeError):
                        size_val = 0
                    if scale != "0" or size_val >= 10:
                        sql_datatype = f"DECIMAL({size},{scale})"
                    elif 1 <= size_val <= 4:
                        sql_datatype = "SMALLINT"
                    elif 5 <= size_val <= 9:
                        sql_datatype = "INTEGER"
                
                field = xf["name"]
                after_clause = f" AFTER {prev_xml_name}" if prev_xml_name else ""
                
                row_data["fix_query"]  = (
                    f"DISABLE INDEX ALL FOR {schema}.{csv_table};\n"
                    f"ALTER TABLE {schema}.{csv_table} ADD COLUMN {field} {sql_datatype}{after_clause};\n"
                    f"REBUILD INDEX ALL FOR {schema}.{csv_table};\n"
                    f"UPDATE STATISTICS {schema}.{csv_table};"
                )
                row_data["highlights"] = set()
                missing_count += 1
        else:
            row_data["csv_order"] = id_value

            if is_occurs_match:
                clean_name        = re.sub(r"_1$", "", csv_info["column_name"], flags=re.IGNORECASE)
                row_data["csv_name"] = f"{clean_name} (Occurs)"
            else:
                row_data["csv_name"] = csv_info["column_name"]

            row_data["csv_type"]  = (csv_info["type"]      or "-").upper()
            row_data["csv_prec"]  =  csv_info["precision"] or "-"
            row_data["csv_scale"] =  csv_info["scale"]     or "-"

            status      = "OK: hidden" if is_hidden else "OK"
            color_class = "ok"

            err_status, err_class, err_highlights = _validate_field(
                xf, row_data["csv_type"], row_data["csv_prec"], row_data["csv_scale"]
            )
            if err_status:
                status      = err_status
                color_class = err_class

            row_data["status"]     = status
            row_data["class"]      = color_class
            row_data["highlights"] = err_highlights if err_status else set()

            # fix_query — monta a query de correção conforme o status
            fix_query = ""
            field     = xf["name"]
            size      = xf["size"]
            scale     = xf["scale"] if xf["scale"] not in ("-", "") else "0"

            # Verifica presença de erros específicos no status composto
            is_char_error    = "expected CHAR/VARCHAR" in status
            is_decimal_error = "expected DECIMAL" in status
            is_alphanum      = xf["type"].lower() == "alphanum"

            if is_char_error or (is_alphanum and "PRECISION MISMATCH" in status):
                # CHAR/VARCHAR — usa xml size como precision
                fix_query = (
                    f"DISABLE INDEX ALL FOR {schema}.{csv_table};\n"
                    f"ALTER TABLE {schema}.{csv_table} MODIFY {field} TO {field} CHAR({size});\n"
                    f"REBUILD INDEX ALL FOR {schema}.{csv_table};\n"
                    f"UPDATE STATISTICS {schema}.{csv_table};"
                )
            elif is_decimal_error or "PRECISION MISMATCH" in status or "SCALE MISMATCH" in status:
                # DECIMAL — usa xml size e scale
                fix_query = (
                    f"DISABLE INDEX ALL FOR {schema}.{csv_table};\n"
                    f"ALTER TABLE {schema}.{csv_table} MODIFY {field} TO {field} DECIMAL({size},{scale});\n"
                    f"REBUILD INDEX ALL FOR {schema}.{csv_table};\n"
                    f"UPDATE STATISTICS {schema}.{csv_table};"
                )
            elif status == "TYPE MISMATCH: expected SMALLINT":
                fix_query = (
                    f"DISABLE INDEX ALL FOR {schema}.{csv_table};\n"
                    f"ALTER TABLE {schema}.{csv_table} MODIFY {field} TO {field} SMALLINT;\n"
                    f"REBUILD INDEX ALL FOR {schema}.{csv_table};\n"
                    f"UPDATE STATISTICS {schema}.{csv_table};"
                )
            elif status == "TYPE MISMATCH: expected INTEGER":
                fix_query = (
                    f"DISABLE INDEX ALL FOR {schema}.{csv_table};\n"
                    f"ALTER TABLE {schema}.{csv_table} MODIFY {field} TO {field} INTEGER;\n"
                    f"REBUILD INDEX ALL FOR {schema}.{csv_table};\n"
                    f"UPDATE STATISTICS {schema}.{csv_table};"
                )
            elif "expected TIMESTAMP/DATE" in status:
                fix_query = (
                    f"DISABLE INDEX ALL FOR {schema}.{csv_table};\n"
                    f"ALTER TABLE {schema}.{csv_table} MODIFY {field} TO {field} TIMESTAMP;\n"
                    f"REBUILD INDEX ALL FOR {schema}.{csv_table};\n"
                    f"UPDATE STATISTICS {schema}.{csv_table};"
                )

            row_data["fix_query"] = fix_query

            if color_class == "ok":
                ok_count += 1
            elif color_class == "error":
                err_count += 1

        prev_xml_name = xf["name"]
        rows.append(row_data)

    return rows, ok_count, err_count, missing_count, ignored_count, matched_occurs_bases


def process_comparison(xml_file, csv_file, csv_filename="", schema_name=""):
    """
    Processa os arquivos XML e CSV e retorna o resumo + lista de rows comparadas.

    Args:
        schema_name: nome do schema informado pelo usuário, usado nas queries
                      de correção (fix_query). Se vazio, usa "SCHEMA" como padrão.

    Returns:
        tuple: (summary_dict, rows_list) em caso de sucesso
               (None, error_string) em caso de erro
    """
    schema = schema_name.strip().upper() if schema_name and schema_name.strip() else "SCHEMA"

    # ── XML ──────────────────────────────────────────────────────────────
    try:
        xml_content = xml_file.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"Erro ao abrir arquivo XML: {e}"

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        return None, f"Erro estrutural no XML: {e}"

    xml_version = _extract_xml_version(xml_content)
    xml_fields  = parse_xml_structure(root)

    if not xml_fields:
        return None, "Nenhum campo válido encontrado no XML."

    # ── CSV ──────────────────────────────────────────────────────────────
    csv_fields = {}
    try:
        csv_stream = io.StringIO(csv_file.read().decode("utf-8", errors="replace"))
        reader     = csv.DictReader(csv_stream)

        if not reader.fieldnames:
            return None, "CSV vazio."

        for row in reader:
            col_raw  = row.get("COLUMN_NAME") or row.get("column_name") or ""
            name_key = col_raw.strip().lower()
            if not name_key:
                continue

            csv_fields[name_key] = {
                "column_name": col_raw.strip(),
                "type":      (row.get("TYPE_NAME")    or row.get("type_name")    or "-").strip(),
                "precision": (row.get("PRECISION")    or row.get("precision")    or "-").strip(),
                "scale":     (row.get("SCALE")        or row.get("scale")        or "-").strip(),
                "order":     str(_safe_int(
                    (row.get("COLUMN_ORDER") or row.get("column_order") or "0").strip()
                )),
            }
    except Exception as e:
        return None, f"Erro ao ler CSV: {e}"

    # ── COMPARAÇÃO ───────────────────────────────────────────────────────
    # Nome da tabela = nome do CSV sem extensão, em maiúsculas
    csv_table = Path(csv_filename).stem.upper() if csv_filename else "TABLE"

    rows, ok_count, err_count, missing_count, ignored_count, matched_occurs_bases = \
        _compare_fields(xml_fields, csv_fields, csv_table, schema)

    # ── SOBRAS DO CSV (não encontradas no XML) ────────────────────────────
    processed_xml_names = {x["name"].lower() for x in xml_fields}

    for key, info in csv_fields.items():
        if key in processed_xml_names:
            continue

        # Ignora cópias extras de campos com occurs (ex: campo_2_1, campo_3_1...)
        # A base já foi validada 1x via matched_occurs_bases (usando o sufixo _1
        # como referência). Qualquer variação com um ou mais sufixos numéricos
        # extras acima dessa base (_2_1, _2_2, _3_1 etc.) segue o mesmo modelo
        # e não precisa ser reavaliada nem exibida.
        match_suffix = re.match(r"^(.+?)(?:_\d+)+$", key)
        if match_suffix and match_suffix.group(1) in matched_occurs_bases:
            continue

        rows.append({
            "id":         "-",
            "xml_name":   "-",
            "xml_type":   "-",
            "xml_size":   "-",
            "xml_scale":  "-",
            "xml_digits": "-",
            "xml_offset": "-",
            "xml_hidden": "-",
            "csv_order":  info["order"],
            "csv_name":   info["column_name"],
            "csv_type":   info["type"],
            "csv_prec":   info["precision"],
            "csv_scale":  info["scale"],
            "status":     "MISSING: field not in XML",
            "class":      "missing",
            "fix_query":  "",
            "highlights": set(),
        })
        missing_count += 1

    summary = {
        "total":     len(rows),
        "ok":        ok_count,
        "errors":    err_count,
        "missing":   missing_count,
        "ignored":   ignored_count,
        "xml_ver":   xml_version,
        "csv_table": csv_table,
        "schema":    schema,
        "date":      datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    return summary, rows


# ---------------------------------------------------------------------------
#  ROTA FLASK
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        schema_name = request.form.get("schema_name", "").strip()

        # #7 — validação server-side (complementa a client-side do JS)
        if "xml_file" not in request.files or "csv_file" not in request.files:
            return render_template("index.html", error="Por favor, suba ambos os arquivos.",
                                   schema_name=schema_name)

        xml   = request.files["xml_file"]
        csv_f = request.files["csv_file"]

        if xml.filename == "" or csv_f.filename == "":
            return render_template("index.html", error="Nenhum arquivo selecionado.",
                                   schema_name=schema_name)

        summary, data = process_comparison(
            xml, csv_f, csv_filename=csv_f.filename, schema_name=schema_name
        )

        if summary is None:
            return render_template("index.html", error=data, schema_name=schema_name)

        return render_template("index.html", summary=summary, rows=data, show_results=True,
                               schema_name=schema_name)

    return render_template("index.html", show_results=False, schema_name="")


if __name__ == "__main__":
    app.run(debug=True, port=5001)
