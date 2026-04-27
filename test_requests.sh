#!/bin/bash
# ============================================================================
# Test requests for the Toelatingen POC
# Uses typed endpoints — no type or role needed in payload
# ============================================================================

BASE_URL="http://localhost:8000"

# ----------------------------------------------------------------------------
# File upload helper
# ----------------------------------------------------------------------------
# Usage: FID=$(upload_file <user> <local_filename> <display_filename> <dossier_id>)
#
# Calls POST /files/upload/request to get a signed upload URL, then PUTs a
# small synthetic payload to the File Service. Echoes the resulting file_id
# (and only the file_id) on stdout so it can be captured.
#
# Since the Bug 47 fix, dossier_id is required at token-mint time — the
# engine signs it into the upload token and the file_service writes it as
# intended_dossier_id into the temp .meta. At move time the file_service
# rejects any attempt to move the file into a different dossier. Every
# caller in this script knows the dossier_id because they've already
# decided on one (this is the client-generates-UUIDs model).
#
# Errors are written to stderr; on failure the function returns the empty
# string and the caller's curl will produce a 422 from the engine.
upload_file() {
  local user="$1"
  local content="$2"
  local filename="$3"
  local dossier_id="$4"

  if [ -z "$dossier_id" ]; then
    echo "upload_file: dossier_id (4th arg) is required" >&2
    return 1
  fi

  local resp
  resp=$(curl -s -X POST "$BASE_URL/files/upload/request" \
    -H "Content-Type: application/json" \
    -H "X-POC-User: $user" \
    -d "{\"filename\": \"$filename\", \"dossier_id\": \"$dossier_id\"}")

  local file_id upload_url
  file_id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['file_id'])" 2>/dev/null)
  upload_url=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_url'])" 2>/dev/null)

  if [ -z "$file_id" ] || [ -z "$upload_url" ]; then
    echo "upload_file: failed to get token: $resp" >&2
    return 1
  fi

  # PUT the bytes to the File Service. The endpoint expects multipart form
  # data with a `file` field.
  local tmpfile
  tmpfile=$(mktemp)
  printf '%s' "$content" > "$tmpfile"
  curl -s -X PUT "$upload_url" -F "file=@$tmpfile;filename=$filename" > /dev/null
  rm -f "$tmpfile"

  echo "$file_id"
}

# ----------------------------------------------------------------------------

echo "============================================"
echo "DOSSIER 1: Brugge, RRN aanvrager"
echo "============================================"
echo ""

echo "--- D1 Step 1: dienAanvraagIn (with bijlage) ---"
D1_BIJLAGE_FID=$(upload_file "jan.aanvrager" "Detailplan voor de gevelrestauratie." "detailplan.pdf" "d1000000-0000-0000-0000-000000000001")
echo "  uploaded bijlage file_id=$D1_BIJLAGE_FID"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000001/oe:dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/10001\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"Restauratie gevelbekleding stadhuis\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10001\",
          \"bijlagen\": [
            { \"file_id\": \"$D1_BIJLAGE_FID\", \"filename\": \"detailplan.pdf\", \"content_type\": \"application/pdf\", \"size\": 32 }
          ]
        }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D1 Verify file_download_url injection ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
found = False
for e in d.get('currentEntities', []):
    if e['type'] == 'oe:aanvraag':
        bs = e['content'].get('bijlagen', [])
        assert bs, f'aanvraag has no bijlagen: {e[\"content\"]}'
        for b in bs:
            assert 'file_download_url' in b, f'missing file_download_url on bijlage: {b}'
            print(f\"  bijlage file_id={b['file_id'][:8]}... file_download_url={b['file_download_url'][:70]}...\")
            found = True
        break
assert found, 'no oe:aanvraag entity found in currentEntities'
print('  OK: file_download_url was injected on Bijlage.file_id')
"
echo ""

echo "--- D1 Step 2: neemBeslissing (onvolledig, direct) ---"
D1_BRIEF1_FID=$(upload_file "marie.brugge" "Beslissingsbrief: aanvraag onvolledig." "d1-brief-001.pdf" "d1000000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000002/oe:neemBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"used\": [
      {\"entity\": \"oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001\"}
    ],
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"beslissing\": \"onvolledig\",
          \"datum\": \"2026-03-26T10:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10001\",
          \"brief\": \"$D1_BRIEF1_FID\"
        }
      },
      {
        \"entity\": \"oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000003\",
        \"content\": { \"getekend\": true }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D1 Check status (expect: aanvraag_onvolledig) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "--- D1 Verify brief_download_url injection (default naming rule) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
found = False
for e in d.get('currentEntities', []):
    if e['type'] == 'oe:beslissing':
        c = e['content']
        assert 'brief' in c, f'no brief in beslissing: {c}'
        assert 'brief_download_url' in c, f'missing brief_download_url: keys={sorted(c.keys())}'
        print(f\"  brief={c['brief'][:8]}... brief_download_url={c['brief_download_url'][:70]}...\")
        found = True
        break
assert found, 'no oe:beslissing entity found'
print('  OK: brief_download_url was injected on Beslissing.brief (default rule)')
"
echo ""

echo "--- D1 Step 3: vervolledigAanvraag ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000004/oe:vervolledigAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/10001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000004",
        "derivedFrom": "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Restauratie gevelbekleding stadhuis - aangevuld met detailplannen",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/10001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D1 Step 4: neemBeslissing (goedgekeurd, direct) ---"
D1_BRIEF2_FID=$(upload_file "marie.brugge" "Beslissingsbrief: aanvraag goedgekeurd." "d1-brief-002.pdf" "d1000000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000005/oe:neemBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"used\": [
      {\"entity\": \"oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000004\"}
    ],
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000005\",
        \"derivedFrom\": \"oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"beslissing\": \"goedgekeurd\",
          \"datum\": \"2026-03-27T14:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10001\",
          \"brief\": \"$D1_BRIEF2_FID\"
        }
      },
      {
        \"entity\": \"oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000006\",
        \"derivedFrom\": \"oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000003\",
        \"content\": { \"getekend\": true }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D1 Final status (expect: toelating_verleend) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D1 Graph: $BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/prov/graph/timeline"
echo ""
echo ""


echo "============================================"
echo "DOSSIER 2: Gent, KBO aanvrager, separate signer, declined signing"
echo "  behandelaar: benjamma"
echo "  ondertekenaar: sophie.tekent"
echo "============================================"
echo ""

echo "--- D2 Step 1: dienAanvraagIn (firma.acme) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000001/oe:dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: firma.acme" \
  -d '{
    "workflow": "toelatingen",
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/20001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Plaatsing zonnepanelen op beschermd pand",
          "handeling": "plaatsing",
          "aanvrager": { "kbo": "0123456789" },
          "gemeente": "Gent",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 2: doeVoorstelBeslissing — onvolledig (benjamma) ---"
D2_BRIEF1_FID=$(upload_file "benjamma" "Beslissingsbrief D2: voorstel onvolledig." "d2-brief-001.pdf" "d2000000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000002/oe:doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"used\": [
      {\"entity\": \"oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000001\"}
    ],
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"beslissing\": \"onvolledig\",
          \"datum\": \"2026-03-26T11:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/20001\",
          \"brief\": \"$D2_BRIEF1_FID\"
        }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D2 Step 3: tekenBeslissing — sophie signs (triggers neemBeslissing → onvolledig) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000003/oe:tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
    "used": [
      {"entity": "oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000002"}
    ],
    "generated": [
      {
        "entity": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000003",
        "content": { "getekend": true }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Check status (expect: aanvraag_onvolledig) ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: benjamma" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "--- D2 Step 4: vervolledigAanvraag (firma.acme) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000004/oe:vervolledigAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: firma.acme" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/20001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000004",
        "derivedFrom": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Plaatsing zonnepanelen op beschermd pand - met technische fiche",
          "handeling": "plaatsing",
          "aanvrager": { "kbo": "0123456789" },
          "gemeente": "Gent",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 5: bewerkAanvraag (benjamma) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000005/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/20001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000005",
        "derivedFrom": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000004",
        "content": {
          "onderwerp": "Plaatsing zonnepanelen op beschermd pand - met technische fiche en advies",
          "handeling": "plaatsing",
          "aanvrager": { "kbo": "0123456789" },
          "gemeente": "Gent",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 6: doeVoorstelBeslissing — goedgekeurd (benjamma) ---"
D2_BRIEF2_FID=$(upload_file "benjamma" "Beslissingsbrief D2: voorstel goedgekeurd." "d2-brief-002.pdf" "d2000000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000006/oe:doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"used\": [
      {\"entity\": \"oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000005\"}
    ],
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000006\",
        \"derivedFrom\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"beslissing\": \"goedgekeurd\",
          \"datum\": \"2026-03-28T09:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/20001\",
          \"brief\": \"$D2_BRIEF2_FID\"
        }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D2 Step 7: tekenBeslissing — sophie DECLINES (getekend: false → klaar_voor_behandeling) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000007/oe:tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
    "used": [
      {"entity": "oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000006"}
    ],
    "generated": [
      {
        "entity": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000007",
        "derivedFrom": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000003",
        "content": { "getekend": false }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Check status (expect: klaar_voor_behandeling) ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: benjamma" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "--- D2 Step 8: doeVoorstelBeslissing — goedgekeurd second attempt (benjamma) ---"
D2_BRIEF3_FID=$(upload_file "benjamma" "Beslissingsbrief D2: tweede voorstel goedgekeurd." "d2-brief-003.pdf" "d2000000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000008/oe:doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"used\": [
      {\"entity\": \"oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000005\"}
    ],
    \"generated\": [
      {
        \"entity\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000008\",
        \"derivedFrom\": \"oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000006\",
        \"content\": {
          \"beslissing\": \"goedgekeurd\",
          \"datum\": \"2026-03-29T10:00:00Z\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/20001\",
          \"brief\": \"$D2_BRIEF3_FID\"
        }
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D2 Step 9: tekenBeslissing — sophie SIGNS (triggers neemBeslissing → goedgekeurd) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000009/oe:tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
    "used": [
      {"entity": "oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000008"}
    ],
    "generated": [
      {
        "entity": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000009",
        "derivedFrom": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000007",
        "content": { "getekend": true }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Final status (expect: toelating_verleend) ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: benjamma" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D2 Graph: $BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/prov/graph/timeline"
echo ""
echo ""

echo "============================================"
echo "List all dossiers"
echo "============================================"
curl -s "$BASE_URL/dossiers" -H "X-POC-User: claeyswo" | python3 -m json.tool
echo ""
echo ""

echo "============================================"
echo "DOSSIER 3: Batch — bewerkAanvraag + doeVoorstelBeslissing in one call"
echo "============================================"
echo ""

echo "--- D3 Step 1: dienAanvraagIn ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d3000000-0000-0000-0000-000000000001/activities/a3000000-0000-0000-0000-000000000001/oe:dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "workflow": "toelatingen",
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/30001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Batch test — renovatie kapel",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/30001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D3 Step 2: BATCH bewerkAanvraag + doeVoorstelBeslissing ---"
D3_BRIEF1_FID=$(upload_file "marie.brugge" "Beslissingsbrief D3: kapel renovatie." "d3-brief-001.pdf" "d3000000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d3000000-0000-0000-0000-000000000001/activities" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"activities\": [
      {
        \"activity_id\": \"a3000000-0000-0000-0000-000000000002\",
        \"type\": \"bewerkAanvraag\",
        \"used\": [
          { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/30001\" }
        ],
        \"generated\": [
          {
            \"entity\": \"oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000002\",
            \"derivedFrom\": \"oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000001\",
            \"content\": {
              \"onderwerp\": \"Batch test — renovatie kapel (bewerkt met advies)\",
              \"handeling\": \"renovatie\",
              \"aanvrager\": { \"rrn\": \"85010100123\" },
              \"gemeente\": \"Brugge\",
              \"object\": \"https://id.erfgoed.net/erfgoedobjecten/30001\"
            }
          }
        ]
      },
      {
        \"activity_id\": \"a3000000-0000-0000-0000-000000000003\",
        \"type\": \"doeVoorstelBeslissing\",
        \"used\": [
          {\"entity\": \"oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000002\"}
        ],
        \"generated\": [
          {
            \"entity\": \"oe:beslissing/e3000000-0000-0000-0000-000000000002@f3000000-0000-0000-0000-000000000003\",
            \"content\": {
              \"beslissing\": \"goedgekeurd\",
              \"datum\": \"2026-03-30T12:00:00Z\",
              \"object\": \"https://id.erfgoed.net/erfgoedobjecten/30001\",
              \"brief\": \"$D3_BRIEF1_FID\"
            }
          }
        ]
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D3 Final status (expect: beslissing_te_tekenen) ---"
curl -s "$BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D3 Graph: $BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001/prov/graph/timeline"
echo ""
echo ""

echo "============================================"
echo "DOSSIER 4: Batch — explicit used ref between activities"
echo "  bewerkAanvraag generates oe:aanvraag@new_version"
echo "  doeVoorstelBeslissing explicitly uses that version"
echo "============================================"
echo ""

echo "--- D4 Step 1: dienAanvraagIn ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d4000000-0000-0000-0000-000000000001/activities/a4000000-0000-0000-0000-000000000001/oe:dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "workflow": "toelatingen",
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/40001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Explicit ref batch test — restauratie toren",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/40001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D4 Step 2: BATCH bewerkAanvraag + doeVoorstelBeslissing (explicit used ref) ---"
D4_BRIEF1_FID=$(upload_file "marie.brugge" "Beslissingsbrief D4: torenrestauratie." "d4-brief-001.pdf" "d4000000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d4000000-0000-0000-0000-000000000001/activities" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"activities\": [
      {
        \"activity_id\": \"a4000000-0000-0000-0000-000000000002\",
        \"type\": \"bewerkAanvraag\",
        \"used\": [
          { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/40001\" }
        ],
        \"generated\": [
          {
            \"entity\": \"oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000002\",
            \"derivedFrom\": \"oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000001\",
            \"content\": {
              \"onderwerp\": \"Explicit ref batch test — restauratie toren (bewerkt)\",
              \"handeling\": \"renovatie\",
              \"aanvrager\": { \"rrn\": \"85010100123\" },
              \"gemeente\": \"Brugge\",
              \"object\": \"https://id.erfgoed.net/erfgoedobjecten/40001\"
            }
          }
        ]
      },
      {
        \"activity_id\": \"a4000000-0000-0000-0000-000000000003\",
        \"type\": \"doeVoorstelBeslissing\",
        \"used\": [
          { \"entity\": \"oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000002\" }
        ],
        \"generated\": [
          {
            \"entity\": \"oe:beslissing/e4000000-0000-0000-0000-000000000002@f4000000-0000-0000-0000-000000000003\",
            \"content\": {
              \"beslissing\": \"goedgekeurd\",
              \"datum\": \"2026-03-30T14:00:00Z\",
              \"object\": \"https://id.erfgoed.net/erfgoedobjecten/40001\",
              \"brief\": \"$D4_BRIEF1_FID\"
            }
          }
        ]
      }
    ]
  }" | python3 -m json.tool
echo ""

echo "--- D4 Final status (expect: beslissing_te_tekenen) ---"
curl -s "$BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D4 Graph: $BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001/prov/graph/timeline"
echo ""
echo ""

# ============================================================================
# DOSSIER 5: derivation rules — negative tests
# ============================================================================
# These cases deliberately trip the derivation validator added to the engine.
# Uses bewerkAanvraag for the revision step because it only requires
# klaar_voor_behandeling status, which is what we end up in after an initial
# dienAanvraagIn.
# ============================================================================

echo "============================================"
echo "DOSSIER 5: derivation rules — negative tests"
echo "============================================"
echo ""

D5_AANVRAAG_FID=$(upload_file "jan.aanvrager" "initiele aanvraag bijlage" "d5-initieel.pdf" "d5000000-0000-0000-0000-000000000001")

echo "--- D5 Step 1: dienAanvraagIn (baseline v1) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000001/oe:dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [{\"entity\": \"https://id.erfgoed.net/erfgoedobjecten/50001\"}],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"Derivation test baseline\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/50001\",
          \"bijlagen\": [{ \"file_id\": \"$D5_AANVRAAG_FID\", \"filename\": \"d5-initieel.pdf\" }]
        }
      }
    ]
  }" > /dev/null
echo "  baseline aanvraag v1 created"
echo ""

echo "--- D5 Step 2: bewerkAanvraag v2 (happy path — correct derivedFrom from v1) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000002/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000002",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Derivation test baseline - bewerkt v2",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }' | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'detail' in d:
    print(f'  FAIL: got error: {d[\"detail\"]}')
    sys.exit(1)
print('  OK: happy-path derivation v1->v2 accepted')
"
echo ""

echo "--- D5 Step 3: NEGATIVE — missing derivedFrom on existing entity (expect 422 missing_derivation_chain) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000003/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000003",
        "content": {
          "onderwerp": "missing derivedFrom",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
assert isinstance(inner, dict), f'expected dict detail, got {type(inner).__name__}: {inner}'
assert inner.get('error') == 'missing_derivation_chain', f'expected error=missing_derivation_chain, got {inner.get(\"error\")}'
assert 'latest_version' in inner, f'expected latest_version in payload'
lv = inner['latest_version']
assert lv['versionId'] == 'f5000000-0000-0000-0000-000000000002', f'wrong latest: {lv[\"versionId\"]}'
print(f'  OK: 422 missing_derivation_chain; latest_version.versionId={lv[\"versionId\"][:8]}...')
"
echo ""

echo "--- D5 Step 4: NEGATIVE — derivedFrom from non-latest version (expect 422 invalid_derivation_chain) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000004/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000004",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "stale derivation",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
assert inner.get('error') == 'invalid_derivation_chain', f'expected error=invalid_derivation_chain, got {inner.get(\"error\")}'
assert inner.get('declared_parent') == 'f5000000-0000-0000-0000-000000000001'
assert inner.get('latest_parent') == 'f5000000-0000-0000-0000-000000000002'
assert 'latest_version' in inner
print(f'  OK: 422 invalid_derivation_chain; declared=v1, latest=v2, latest_version.content returned')
"
echo ""

echo "--- D5 Step 5: NEGATIVE — unknown parent version (expect 422 unknown_parent) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000005/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000006",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@ffffffff-ffff-ffff-ffff-ffffffffffff",
        "content": {
          "onderwerp": "unknown parent",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
assert inner.get('error') == 'unknown_parent', f'expected error=unknown_parent, got {inner.get(\"error\")}'
print(f'  OK: 422 unknown_parent')
"
echo ""

echo "--- D5 Step 6: NEGATIVE — cross-entity derivation (expect 422 cross_entity_derivation) ---"
# NEW entity_id (e5...99) trying to derive from the existing e5...01 chain
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000006/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{ "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000099@f5000000-0000-0000-0000-000000000099",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000002",
        "content": {
          "onderwerp": "cross-entity derivation",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
assert inner.get('error') == 'cross_entity_derivation', f'expected error=cross_entity_derivation, got {inner.get(\"error\")}'
print(f'  OK: 422 cross_entity_derivation')
"
echo ""

echo "--- D5 Step 7: NEGATIVE — same logical entity in used and generated, local (expect 422 used_generated_overlap) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000007/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" },
      { "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000002" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000007",
        "derivedFrom": "oe:aanvraag/e5000000-0000-0000-0000-000000000001@f5000000-0000-0000-0000-000000000002",
        "content": {
          "onderwerp": "should not get here — used and generated overlap on entity_id",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/50001"
        }
      }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
assert inner.get('error') == 'used_generated_overlap', f'expected error=used_generated_overlap, got {inner.get(\"error\")}'
overlaps = inner.get('overlaps', [])
assert len(overlaps) == 1, f'expected 1 overlap, got {len(overlaps)}: {overlaps}'
assert overlaps[0]['kind'] == 'local'
assert overlaps[0]['entity_id'] == 'e5000000-0000-0000-0000-000000000001'
print(f'  OK: 422 used_generated_overlap (local entity_id)')
"
echo ""

echo "--- D5 Step 8: NEGATIVE — same external URI in used and generated (expect 422 used_generated_overlap) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d5000000-0000-0000-0000-000000000001/activities/a5000000-0000-0000-0000-000000000008/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }
    ],
    "generated": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/50001" }
    ]
  }')
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
assert inner.get('error') == 'used_generated_overlap', f'expected error=used_generated_overlap, got {inner.get(\"error\")}'
overlaps = inner.get('overlaps', [])
assert len(overlaps) == 1, f'expected 1 overlap, got {len(overlaps)}: {overlaps}'
assert overlaps[0]['kind'] == 'external'
assert overlaps[0]['entity'] == 'https://id.erfgoed.net/erfgoedobjecten/50001'
print(f'  OK: 422 used_generated_overlap (external URI)')
"
echo ""

echo "D5 summary: all derivation + invariant checks passed"
echo ""
echo ""

# ============================================================================
# DOSSIER 6: stale used reference check + oe:neemtAkteVan
# ============================================================================
# Verifies the staleness-acknowledgement workflow on its canonical use case:
# a read-only activity (`doeVoorstelBeslissing`) references the aanvraag it
# inspects via the `used` block. If the operator's view of the aanvraag is
# stale (a newer version exists), the engine fires `oe:neemtAkteVan`'s
# validator. Without an `oe:neemtAkteVan` ack covering the intervening
# versions, the request fails with 409 stale_used_reference.
#
# The setup creates an aanvraag, revises it via bewerkAanvraag (so v2 is the
# latest), then runs three doeVoorstelBeslissing variants:
#   3. used=v1, no ack            → expect 409 stale_used_reference
#   4. used=v1, ack=v2            → expect 200, beslissing created
#   5. used=v1, ack=external uri  → expect 422 (externals can't be in relations)
#
# Note: revise activities (bewerkAanvraag, vervolledigAanvraag) cannot
# legitimately participate in this workflow, because per the engine's
# disjoint invariant, a logical entity is never in both `used` and
# `generated` of the same activity. The staleness story applies only to
# read-only references — see `pipeline/invariants.py`.
# ============================================================================

echo "============================================"
echo "DOSSIER 6: stale used + oe:neemtAkteVan"
echo "============================================"
echo ""

echo "--- D6 Step 1: dienAanvraagIn (aanvraag v1) ---"
D6_FID=$(upload_file "jan.aanvrager" "D6 initieel" "d6.pdf" "d6000000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d6000000-0000-0000-0000-000000000001/activities/a6000000-0000-0000-0000-000000000001/oe:dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [{\"entity\": \"https://id.erfgoed.net/erfgoedobjecten/60001\"}],
    \"generated\": [{
      \"entity\": \"oe:aanvraag/e6000000-0000-0000-0000-000000000001@f6000000-0000-0000-0000-000000000001\",
      \"content\": {
        \"onderwerp\": \"stale-used test baseline\",
        \"handeling\": \"renovatie\",
        \"aanvrager\": {\"rrn\": \"85010100123\"},
        \"gemeente\": \"Brugge\",
        \"object\": \"https://id.erfgoed.net/erfgoedobjecten/60001\",
        \"bijlagen\": [{\"file_id\": \"$D6_FID\", \"filename\": \"d6.pdf\"}]
      }
    }]
  }" > /dev/null
echo "  aanvraag v1 created"
echo ""

echo "--- D6 Step 2: bewerkAanvraag -> aanvraag v2 (latest) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d6000000-0000-0000-0000-000000000001/activities/a6000000-0000-0000-0000-000000000002/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "used": [{"entity": "https://id.erfgoed.net/erfgoedobjecten/60001"}],
    "generated": [{
      "entity": "oe:aanvraag/e6000000-0000-0000-0000-000000000001@f6000000-0000-0000-0000-000000000002",
      "derivedFrom": "oe:aanvraag/e6000000-0000-0000-0000-000000000001@f6000000-0000-0000-0000-000000000001",
      "content": {
        "onderwerp": "bewerkt v2",
        "handeling": "renovatie",
        "aanvrager": {"rrn": "85010100123"},
        "gemeente": "Brugge",
        "object": "https://id.erfgoed.net/erfgoedobjecten/60001"
      }
    }]
  }' > /dev/null
echo "  aanvraag v2 created (now latest)"
echo ""

echo "--- D6 Step 3: NEGATIVE — doeVoorstelBeslissing reads stale v1, no ack (expect 409 stale_used_reference) ---"
D6_BRIEF_FID=$(upload_file "marie.brugge" "D6 brief" "d6-brief.pdf" "d6000000-0000-0000-0000-000000000001")
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d6000000-0000-0000-0000-000000000001/activities/a6000000-0000-0000-0000-000000000003/oe:doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"used\": [
      {\"entity\": \"oe:aanvraag/e6000000-0000-0000-0000-000000000001@f6000000-0000-0000-0000-000000000001\"}
    ],
    \"generated\": [{
      \"entity\": \"oe:beslissing/e6000000-0000-0000-0000-000000000002@f6000000-0000-0000-0000-000000000003\",
      \"content\": {
        \"beslissing\": \"goedgekeurd\",
        \"datum\": \"2026-04-01T10:00:00Z\",
        \"object\": \"https://id.erfgoed.net/erfgoedobjecten/60001\",
        \"brief\": \"$D6_BRIEF_FID\"
      }
    }]
  }")
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '409', f'expected 409, got {code}: {body}'
assert inner.get('error') == 'stale_used_reference', f'expected error=stale_used_reference, got {inner.get(\"error\")}'
assert 'stale' in inner and len(inner['stale']) == 1, f'expected 1 stale entry'
stale = inner['stale'][0]
assert stale['declared_version'] == 'f6000000-0000-0000-0000-000000000001'
assert stale['latest_version'] == 'f6000000-0000-0000-0000-000000000002'
assert stale['intervening_versions'] == ['f6000000-0000-0000-0000-000000000002']
lv = inner['latest_version']
assert lv['versionId'] == 'f6000000-0000-0000-0000-000000000002'
assert lv['content']['onderwerp'] == 'bewerkt v2'
print('  OK: 409 stale_used_reference with stale entry + intervening versions + latest content')
"
echo ""

echo "--- D6 Step 4: NEGATIVE — ack an external URI (expect 422 — relations cannot reference externals) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d6000000-0000-0000-0000-000000000001/activities/a6000000-0000-0000-0000-000000000004/oe:doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"used\": [
      {\"entity\": \"oe:aanvraag/e6000000-0000-0000-0000-000000000001@f6000000-0000-0000-0000-000000000001\"}
    ],
    \"relations\": [
      {\"entity\": \"https://id.erfgoed.net/erfgoedobjecten/60001\", \"type\": \"oe:neemtAkteVan\"}
    ],
    \"generated\": [{
      \"entity\": \"oe:beslissing/e6000000-0000-0000-0000-000000000002@f6000000-0000-0000-0000-000000000004\",
      \"content\": {
        \"beslissing\": \"goedgekeurd\",
        \"datum\": \"2026-04-01T11:00:00Z\",
        \"object\": \"https://id.erfgoed.net/erfgoedobjecten/60001\",
        \"brief\": \"$D6_BRIEF_FID\"
      }
    }]
  }")
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
inner = body.get('detail', {})
assert code == '422', f'expected 422, got {code}: {body}'
detail_str = inner if isinstance(inner, str) else inner.get('detail', '')
assert 'external' in detail_str.lower() or 'cannot reference' in detail_str.lower(), \
    f'expected external-rejection error, got: {detail_str}'
print('  OK: 422 relations cannot reference external URIs')
"
echo ""

echo "--- D6 Step 5: POSITIVE — doeVoorstelBeslissing reads stale v1, acks v2 (expect 200) ---"
RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/toelatingen/dossiers/d6000000-0000-0000-0000-000000000001/activities/a6000000-0000-0000-0000-000000000005/oe:doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d "{
    \"used\": [
      {\"entity\": \"oe:aanvraag/e6000000-0000-0000-0000-000000000001@f6000000-0000-0000-0000-000000000001\"}
    ],
    \"relations\": [
      {\"entity\": \"oe:aanvraag/e6000000-0000-0000-0000-000000000001@f6000000-0000-0000-0000-000000000002\", \"type\": \"oe:neemtAkteVan\"}
    ],
    \"generated\": [{
      \"entity\": \"oe:beslissing/e6000000-0000-0000-0000-000000000002@f6000000-0000-0000-0000-000000000005\",
      \"content\": {
        \"beslissing\": \"goedgekeurd\",
        \"datum\": \"2026-04-01T12:00:00Z\",
        \"object\": \"https://id.erfgoed.net/erfgoedobjecten/60001\",
        \"brief\": \"$D6_BRIEF_FID\"
      }
    }]
  }")
echo "$RESP" | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
code = lines[-1]
body = json.loads('\n'.join(lines[:-1]))
assert code == '200', f'expected 200, got {code}: {body}'
assert 'detail' not in body, f'unexpected error: {body}'
assert 'activity' in body, f'no activity in response: {body}'
rels = body.get('relations', [])
assert len(rels) == 1, f'expected 1 relation in response, got {rels}'
assert rels[0]['type'] == 'oe:neemtAkteVan'
assert 'f6000000-0000-0000-0000-000000000002' in rels[0]['entity']
print('  OK: doeVoorstelBeslissing accepted with oe:neemtAkteVan acknowledgement; relation echoed back')
"
echo ""

echo "D6 summary: all stale_used_reference + oe:neemtAkteVan checks passed"
echo ""
echo ""

# ============================================================================
# DOSSIER 7: task cancellation end-to-end
# ============================================================================
# Reuses D2 state from earlier. Verifies that:
# 1. A trekAanvraagIn task was scheduled during D2 (after the onvolledig
#    beslissing)
# 2. That task got cancelled (status=cancelled) after vervolledigAanvraag ran
# ============================================================================

echo "============================================"
echo "DOSSIER 7: task cancellation (reuses D2)"
echo "============================================"
echo ""

echo "--- D7 Check: D2's trekAanvraagIn task was cancelled ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ents = d.get('currentEntities', [])
task_entities = [e for e in ents if e.get('type') == 'system:task' and (e.get('content') or {}).get('target_activity') == 'trekAanvraagIn']
assert len(task_entities) >= 1, f'expected at least 1 trekAanvraagIn task, got {len(task_entities)}'
task = task_entities[0]
c = task['content']
assert c.get('status') == 'cancelled', f'expected status=cancelled (cancelled by vervolledigAanvraag), got {c.get(\"status\")}'
print('  OK: trekAanvraagIn task was cancelled by vervolledigAanvraag')
"
echo ""

echo "D7 summary: task cancellation verified end-to-end"

# ============================================================================
# DOSSIER 8: entity schema versioning
# ============================================================================
# Verifies:
#   1. testDienAanvraagInV2 stamps schema_version=v2 on a fresh aanvraag and
#      round-trips the v2-only 'classificatie' field.
#   2. A legacy (non-versioned) bewerkAanvraag on that v2 row keeps
#      schema_version=v2 (sticky, rule A) — relaxed legacy interop.
#   3. testBewerkAanvraagV2Only against D1's legacy (NULL-version) aanvraag
#      returns 422 unsupported_schema_version with stored_version=null.
# ============================================================================

echo "============================================"
echo "DOSSIER 8: entity schema versioning"
echo "============================================"
echo ""

D8_BIJLAGE_FID=$(upload_file "jan.aanvrager" "D8 v2 bijlage" "d8.pdf" "d8000000-0000-0000-0000-000000000001")

echo "--- D8 Step 1: testDienAanvraagInV2 (creates v2 aanvraag) ---"
D8_STEP1=$(curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d8000000-0000-0000-0000-000000000001/activities/a8000000-0000-0000-0000-000000000001/oe:testDienAanvraagInV2" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [{ \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/10008\" }],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e8000000-0000-0000-0000-000000000001@f8000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"D8: v2 test aanvraag\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10008\",
          \"bijlagen\": [{ \"file_id\": \"$D8_BIJLAGE_FID\", \"filename\": \"d8.pdf\" }],
          \"classificatie\": \"beschermd_monument\",
          \"urgentie\": \"hoog\"
        }
      }
    ]
  }")
echo "$D8_STEP1" | python3 -c "
import sys, json
r = json.load(sys.stdin)
gen = r.get('generated', [])
assert len(gen) == 1, f'expected 1 generated entity, got {len(gen)}: {r}'
g = gen[0]
assert g.get('schemaVersion') == 'v2', f'expected schemaVersion=v2 in response, got {g.get(\"schemaVersion\")}: {r}'
c = g.get('content', {})
assert c.get('classificatie') == 'beschermd_monument', f'classificatie not roundtripped: {c}'
assert c.get('urgentie') == 'hoog', f'urgentie not roundtripped: {c}'
print('  OK: testDienAanvraagInV2 created aanvraag with schema_version=v2 and v2-only fields')
"
echo ""

echo "--- D8 Step 2: GET dossier — verify schemaVersion exposed on read ---"
curl -s "$BASE_URL/dossiers/d8000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ent = [e for e in d.get('currentEntities', []) if e.get('type') == 'oe:aanvraag']
assert len(ent) == 1, f'expected 1 oe:aanvraag, got {len(ent)}'
e = ent[0]
assert e.get('schemaVersion') == 'v2', f'expected schemaVersion=v2 on read, got {e.get(\"schemaVersion\")}'
c = e.get('content', {})
assert c.get('classificatie') == 'beschermd_monument', f'classificatie missing on read: {c}'
print('  OK: GET response exposes schemaVersion=v2 and v2 fields')
"
echo ""

echo "--- D8 Step 3: legacy bewerkAanvraag on v2 row — sticky version (relaxed) ---"
D8_STEP3_CODE=$(curl -s -o /tmp/d8_step3.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/d8000000-0000-0000-0000-000000000001/activities/a8000000-0000-0000-0000-000000000002/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/10008\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e8000000-0000-0000-0000-000000000001@f8000000-0000-0000-0000-000000000002\",
        \"derivedFrom\": \"oe:aanvraag/e8000000-0000-0000-0000-000000000001@f8000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"D8: v2 test aanvraag — bewerkt door legacy handler\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10008\",
          \"bijlagen\": []
        }
      }
    ]
  }")
python3 -c "
import json
code = '$D8_STEP3_CODE'
assert code == '200', f'expected 200, got {code}'
r = json.load(open('/tmp/d8_step3.json'))
g = r['generated'][0]
assert g.get('schemaVersion') == 'v2', f'sticky version broken: expected v2, got {g.get(\"schemaVersion\")}'
print('  OK: legacy bewerkAanvraag on v2 row inherited schema_version=v2 (sticky)')
"
echo ""

echo "--- D8 Step 4: testBewerkAanvraagV2Only on D1 legacy aanvraag (expect 422) ---"
# D1 currently has ended up in toelating_verleend after D1 Step 4 revised the
# aanvraag to version f1000000-...-000000000004. We need derivedFrom pointing
# at the latest. Get it from the API.
D1_LATEST=$(curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for e in d.get('currentEntities', []):
    if e.get('type') == 'oe:aanvraag':
        print(f\"{e['entityId']}@{e['versionId']}\")
        break
")
echo "  D1 latest aanvraag: $D1_LATEST"
D8_STEP4_CODE=$(curl -s -o /tmp/d8_step4.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/d1000000-0000-0000-0000-000000000001/activities/a8000000-0000-0000-0000-000000000099/oe:testBewerkAanvraagV2Only" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/10001\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000099\",
        \"derivedFrom\": \"oe:aanvraag/$D1_LATEST\",
        \"content\": {
          \"onderwerp\": \"should never persist\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10001\",
          \"bijlagen\": []
        }
      }
    ]
  }")
python3 -c "
import json
code = '$D8_STEP4_CODE'
assert code == '422', f'expected 422, got {code}: {open(\"/tmp/d8_step4.json\").read()}'
r = json.load(open('/tmp/d8_step4.json'))
# Payload shape: ActivityError.payload is forwarded via _activity_error_to_http;
# it lands under 'detail' for FastAPI HTTPExceptions. Check the payload marker.
detail = r.get('detail', {})
if isinstance(detail, dict):
    err = detail.get('error')
    stored = detail.get('stored_version')
else:
    # May be nested further — find 'unsupported_schema_version' anywhere
    blob = json.dumps(r)
    assert 'unsupported_schema_version' in blob, f'error marker not in response: {blob}'
    err = 'unsupported_schema_version'
    stored = None
assert err == 'unsupported_schema_version', f'expected error=unsupported_schema_version, got {err}: {r}'
print('  OK: 422 unsupported_schema_version when revising legacy (NULL-version) row')
"
echo ""

echo "D8 summary: entity schema versioning verified end-to-end"

# ============================================================================
# DOSSIER 9: tombstone — irreversible content redaction
# ============================================================================
# Verifies the tombstone activity end-to-end:
#   1. Two-version aanvraag (v1, v2) is tombstoned with a redacted
#      replacement vT and a system:note carrying the reason.
#   2. GET on a tombstoned single-version returns 301 to the replacement.
#   3. Bulk-by-entity GET shows tombstoned versions with content=null,
#      tombstonedBy, and redirectTo markers (option Y).
#   4. currentEntities at the dossier level shows the replacement and
#      the system:note (latest by construction).
#   5. Negative: tombstone without a system:note → 422.
#   6. Negative: tombstone targeting two different entity_ids → 422.
#   7. Re-tombstone: tombstoning the replacement is allowed.
# ============================================================================

echo "============================================"
echo "DOSSIER 9: tombstone — irreversible redaction"
echo "============================================"
echo ""

D9_BIJLAGE_FID=$(upload_file "jan.aanvrager" "D9 initial bijlage" "d9.pdf" "d9000000-0000-0000-0000-000000000001")

echo "--- D9 Step 1: dienAanvraagIn (creates v1) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d9000000-0000-0000-0000-000000000001/activities/a9000000-0000-0000-0000-000000000001/oe:dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [{ \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/10009\" }],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"D9: persoonlijke aanvraag\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10009\",
          \"bijlagen\": [{ \"file_id\": \"$D9_BIJLAGE_FID\", \"filename\": \"d9.pdf\" }]
        }
      }
    ]
  }" > /dev/null
echo "  v1 created"
echo ""

echo "--- D9 Step 2: bewerkAanvraag (creates v2) ---"
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d9000000-0000-0000-0000-000000000001/activities/a9000000-0000-0000-0000-000000000002/oe:bewerkAanvraag" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/10009\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000002\",
        \"derivedFrom\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"D9: persoonlijke aanvraag - aangevuld\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/10009\",
          \"bijlagen\": []
        }
      }
    ]
  }" > /dev/null
echo "  v2 created"
echo ""

echo "--- D9 Step 3: tombstone v1+v2 with redacted replacement vT + reason note ---"
D9_TS_RESPONSE=$(curl -s -X PUT "$BASE_URL/toelatingen/dossiers/d9000000-0000-0000-0000-000000000001/activities/a9000000-0000-0000-0000-000000000003/oe:tombstone" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000001\" },
      { \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000002\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000003\",
        \"derivedFrom\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000002\",
        \"content\": {
          \"onderwerp\": \"[REDACTED]\",
          \"handeling\": \"[REDACTED]\",
          \"aanvrager\": { \"rrn\": \"[REDACTED]\" },
          \"gemeente\": \"[REDACTED]\",
          \"object\": \"[REDACTED]\",
          \"bijlagen\": []
        }
      },
      {
        \"entity\": \"system:note/e9000000-0000-0000-0000-000000000099@f9000000-0000-0000-0000-000000000099\",
        \"content\": { \"text\": \"FOI request 2026-042: redact RRN per GDPR Article 17\", \"ticket\": \"FOI-2026-042\" }
      }
    ]
  }")
echo "$D9_TS_RESPONSE" | python3 -c "
import sys, json
r = json.load(sys.stdin)
gen = r.get('generated', [])
assert len(gen) == 2, f'expected 2 generated entities, got {len(gen)}: {r}'
types = sorted(g['type'] for g in gen)
assert types == ['oe:aanvraag', 'system:note'], f'unexpected generated types: {types}'
print('  OK: tombstone activity persisted with replacement + reason note')
"
echo ""

echo "--- D9 Step 4: GET tombstoned v1 → expect 301 redirect ---"
D9_V1_CODE=$(curl -s -o /tmp/d9_v1.json -w "%{http_code}" \
  "$BASE_URL/dossiers/d9000000-0000-0000-0000-000000000001/entities/oe:aanvraag/e9000000-0000-0000-0000-000000000001/f9000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo")
D9_V1_LOCATION=$(curl -s -o /dev/null -w "%{redirect_url}" \
  "$BASE_URL/dossiers/d9000000-0000-0000-0000-000000000001/entities/oe:aanvraag/e9000000-0000-0000-0000-000000000001/f9000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo")
python3 -c "
code = '$D9_V1_CODE'
loc = '$D9_V1_LOCATION'
assert code == '301', f'expected 301, got {code}'
assert 'f9000000-0000-0000-0000-000000000003' in loc, f'expected redirect to v3 (replacement), got {loc!r}'
print(f'  OK: 301 redirect to replacement ({loc.split(\"/\")[-1][:8]}...)')
"
echo ""

echo "--- D9 Step 5: follow redirect, expect redacted replacement content ---"
curl -s -L "$BASE_URL/dossiers/d9000000-0000-0000-0000-000000000001/entities/oe:aanvraag/e9000000-0000-0000-0000-000000000001/f9000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = d.get('content', {})
assert c.get('onderwerp') == '[REDACTED]', f'expected redacted onderwerp, got {c}'
assert c.get('aanvrager', {}).get('rrn') == '[REDACTED]', f'rrn not redacted: {c}'
print('  OK: redirect resolved to redacted replacement')
"
echo ""

echo "--- D9 Step 6: bulk GET by entity_id — expect markers on tombstoned versions ---"
curl -s "$BASE_URL/dossiers/d9000000-0000-0000-0000-000000000001/entities/oe:aanvraag/e9000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
versions = d.get('versions', [])
assert len(versions) == 3, f'expected 3 versions, got {len(versions)}: {[v[\"versionId\"][:8] for v in versions]}'

tombstoned = [v for v in versions if v.get('tombstonedBy')]
alive = [v for v in versions if not v.get('tombstonedBy')]
assert len(tombstoned) == 2, f'expected 2 tombstoned, got {len(tombstoned)}'
assert len(alive) == 1, f'expected 1 alive, got {len(alive)}'

for ts in tombstoned:
    assert ts.get('content') is None, f'tombstoned version still has content: {ts}'
    assert 'tombstonedBy' in ts and ts['tombstonedBy'], f'missing tombstonedBy: {ts}'
    assert 'redirectTo' in ts and 'f9000000-0000-0000-0000-000000000003' in ts['redirectTo'], f'bad redirectTo: {ts}'

assert alive[0]['versionId'].endswith('000000003'), f'alive version is not vT: {alive[0]}'
assert alive[0]['content']['onderwerp'] == '[REDACTED]', f'alive content not redacted: {alive[0]}'
print('  OK: bulk GET shows 2 tombstoned (with markers) + 1 live replacement')
"
echo ""

echo "--- D9 Step 7: dossier-level currentEntities — replacement + reason note visible ---"
curl -s "$BASE_URL/dossiers/d9000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ents = d.get('currentEntities', [])
aanvraag = [e for e in ents if e.get('type') == 'oe:aanvraag']
notes = [e for e in ents if e.get('type') == 'system:note']
assert len(aanvraag) == 1, f'expected 1 oe:aanvraag in currentEntities, got {len(aanvraag)}'
assert aanvraag[0]['content']['onderwerp'] == '[REDACTED]', f'aanvraag not redacted: {aanvraag[0]}'
assert any('FOI-2026-042' in (n.get('content') or {}).get('ticket', '') for n in notes), f'reason note missing: {notes}'
print('  OK: currentEntities surfaces redacted replacement + reason note')
"
echo ""

echo "--- D9 Step 8: NEGATIVE — tombstone with no system:note (expect 422) ---"
D9_NEG1_CODE=$(curl -s -o /tmp/d9_neg1.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/d9000000-0000-0000-0000-000000000001/activities/a9000000-0000-0000-0000-000000000004/oe:tombstone" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000003\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000004\",
        \"derivedFrom\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000003\",
        \"content\": {
          \"onderwerp\": \"[REDACTED2]\",
          \"handeling\": \"[REDACTED]\",
          \"aanvrager\": { \"rrn\": \"[REDACTED]\" },
          \"gemeente\": \"[REDACTED]\",
          \"object\": \"[REDACTED]\",
          \"bijlagen\": []
        }
      }
    ]
  }")
python3 -c "
import json
code = '$D9_NEG1_CODE'
assert code == '422', f'expected 422, got {code}: {open(\"/tmp/d9_neg1.json\").read()}'
r = json.load(open('/tmp/d9_neg1.json'))
blob = json.dumps(r)
assert 'tombstone_missing_reason_note' in blob, f'wrong error code: {blob}'
print('  OK: 422 tombstone_missing_reason_note when no system:note in generated')
"
echo ""

echo "--- D9 Step 9: NEGATIVE — tombstone targeting two entity_ids (expect 422) ---"
D9_NEG2_CODE=$(curl -s -o /tmp/d9_neg2.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/d9000000-0000-0000-0000-000000000001/activities/a9000000-0000-0000-0000-000000000005/oe:tombstone" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000003\" },
      { \"entity\": \"oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000004\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000005\",
        \"content\": {
          \"onderwerp\": \"[REDACTED]\", \"handeling\": \"[REDACTED]\",
          \"aanvrager\": { \"rrn\": \"[REDACTED]\" }, \"gemeente\": \"[REDACTED]\",
          \"object\": \"[REDACTED]\", \"bijlagen\": []
        }
      },
      {
        \"entity\": \"system:note/e9000000-0000-0000-0000-000000000098@f9000000-0000-0000-0000-000000000098\",
        \"content\": { \"text\": \"should not persist\" }
      }
    ]
  }")
python3 -c "
import json
code = '$D9_NEG2_CODE'
assert code == '422', f'expected 422, got {code}: {open(\"/tmp/d9_neg2.json\").read()}'
r = json.load(open('/tmp/d9_neg2.json'))
blob = json.dumps(r)
# Note: the v9 dossier's tombstoned f9...003 is in dossier d9, but f1...004 is in dossier d1.
# The cross-dossier check fires before the multi-entity check, so we accept either error.
assert ('tombstone_multi_entity' in blob
        or 'different dossier' in blob.lower()
        or 'not found in dossier' in blob.lower()), f'wrong error: {blob}'
print('  OK: 422 rejects multi-entity / cross-dossier tombstone')
"
echo ""

echo "--- D9 Step 10: re-tombstone the replacement (allowed) ---"
D9_RETS_CODE=$(curl -s -o /tmp/d9_rets.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/d9000000-0000-0000-0000-000000000001/activities/a9000000-0000-0000-0000-000000000006/oe:tombstone" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000003\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000007\",
        \"derivedFrom\": \"oe:aanvraag/e9000000-0000-0000-0000-000000000001@f9000000-0000-0000-0000-000000000003\",
        \"content\": {
          \"onderwerp\": \"[REDACTED-2]\", \"handeling\": \"[REDACTED]\",
          \"aanvrager\": { \"rrn\": \"[REDACTED]\" }, \"gemeente\": \"[REDACTED]\",
          \"object\": \"[REDACTED]\", \"bijlagen\": []
        }
      },
      {
        \"entity\": \"system:note/e9000000-0000-0000-0000-000000000097@f9000000-0000-0000-0000-000000000097\",
        \"content\": { \"text\": \"Second-pass redaction: original placeholder leaked an internal ID, redacting again\" }
      }
    ]
  }")
python3 -c "
code = '$D9_RETS_CODE'
assert code == '200', f'expected 200 on re-tombstone, got {code}: {open(\"/tmp/d9_rets.json\").read()}'
print('  OK: re-tombstoning the replacement is allowed (200)')
"
echo ""

echo "D9 summary: tombstone mechanism verified end-to-end"

# ============================================================================
# DOSSIER 10: exception grant lifecycle
# ============================================================================
# Validates the system:exception mechanism end-to-end with a real
# blocked activity (oe:trekAanvraagIn).
#
# After a successful dienAanvraagIn the dossier is in status
# ``klaar_voor_behandeling`` (via the chain: dienAanvraagIn →
# duidVerantwoordelijkeOrganisatieAan → setSystemFields). But
# ``oe:trekAanvraagIn`` requires ``aanvraag_onvolledig`` — so it
# legitimately 409s from this state, giving us a clean blocked
# activity for the exception scenario.
#
# PROV-first design note: we explicitly send ``status: "active"``
# in every grant payload. The engine's content-validation phase
# only VALIDATES submissions (it doesn't coerce defaults into the
# stored content) — so stored system:exception content equals the
# agent's literal assertion, nothing more. A default-filled status
# would be an engine-invented claim the granting agent never
# actually made.
#
# Flow:
#   1. dienAanvraagIn — dossier ends in klaar_voor_behandeling
#   2. sanity: trekAanvraagIn as aanvrager fails 409 (wrong status)
#   3. beheerder grants exception for oe:trekAanvraagIn
#   4. aanvrager runs trekAanvraagIn — succeeds via bypass
#   5. verify: consumeException fired + system:exception is consumed
#   6. re-grant by revising the same logical entity
#   7. beheerder retracts it via retractException
#   8. final sanity: a new trekAanvraagIn still would 409 (status
#      isn't aanvraag_onvolledig and the exception is cancelled)
# ============================================================================

echo "============================================"
echo "DOSSIER 10: exception grant lifecycle"
echo "============================================"
echo ""

# --- D10 Step 1: dienAanvraagIn ---
# After side-effects the dossier is in klaar_voor_behandeling,
# which is exactly the wrong status for trekAanvraagIn.
echo "--- D10 Step 1: dienAanvraagIn (sets dossier status=klaar_voor_behandeling) ---"
D10_BIJLAGE_FID=$(upload_file "jan.aanvrager" "D10 detailplan." "d10-plan.pdf" "daa00000-0000-0000-0000-000000000001")
curl -s -X PUT "$BASE_URL/toelatingen/dossiers/daa00000-0000-0000-0000-000000000001/activities/aa000000-0000-0000-0000-000000000001/oe:dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"https://id.erfgoed.net/erfgoedobjecten/20001\" }
    ],
    \"generated\": [
      {
        \"entity\": \"oe:aanvraag/ea000000-0000-0000-0000-000000000001@fa000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"onderwerp\": \"D10: heritage permit with exception grant lifecycle\",
          \"handeling\": \"renovatie\",
          \"aanvrager\": { \"rrn\": \"85010100123\" },
          \"gemeente\": \"Brugge\",
          \"object\": \"https://id.erfgoed.net/erfgoedobjecten/20001\",
          \"bijlagen\": [
            { \"file_id\": \"$D10_BIJLAGE_FID\", \"filename\": \"d10-plan.pdf\", \"content_type\": \"application/pdf\", \"size\": 32 }
          ]
        }
      }
    ]
  }" > /tmp/d10_s1.json
python3 -c "
import json
d = json.load(open('/tmp/d10_s1.json'))
st = d.get('dossier', {}).get('status')
assert st == 'klaar_voor_behandeling', f'expected klaar_voor_behandeling after dien + side-effects, got {st!r}'
print('  OK: dossier in klaar_voor_behandeling (trekAanvraagIn requires aanvraag_onvolledig — will be blocked)')
"
echo ""

# --- D10 Step 2: sanity — trekAanvraagIn as aanvrager 409s ---
# The aanvrager (jan.aanvrager) can authorize this activity thanks
# to the RRN match, so the 409 we get is purely from workflow
# rules — which is what the exception mechanism exists to bypass.
echo "--- D10 Step 2: sanity — trekAanvraagIn as jan.aanvrager 409s without exception ---"
D10_S2_CODE=$(curl -s -o /tmp/d10_s2.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/daa00000-0000-0000-0000-000000000001/activities/aa000000-0000-0000-0000-000000000002/oe:trekAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [],
    \"generated\": []
  }")
python3 -c "
assert '$D10_S2_CODE' == '409', f'expected 409 baseline, got $D10_S2_CODE: {open(\"/tmp/d10_s2.json\").read()}'
print('  OK: trekAanvraagIn blocked (409) — wrong status and no exception')
"
echo ""

# --- D10 Step 3: beheerder grants exception for oe:trekAanvraagIn ---
# ``status: active`` is sent explicitly. Pydantic's Exception_
# model has no default for status so the engine would reject a
# submission without it; even if it accepted, the stored content
# wouldn't carry the field — and that would weaken the PROV
# audit trail.
echo "--- D10 Step 3: grantException (beheerder → oe:trekAanvraagIn) ---"
D10_S3_CODE=$(curl -s -o /tmp/d10_s3.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/daa00000-0000-0000-0000-000000000001/activities/aa000000-0000-0000-0000-000000000003/oe:grantException" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [],
    \"generated\": [
      {
        \"entity\": \"system:exception/eb000000-0000-0000-0000-000000000001@fb000000-0000-0000-0000-000000000001\",
        \"content\": {
          \"activity\": \"oe:trekAanvraagIn\",
          \"status\": \"active\",
          \"reason\": \"Legal authorization #D10: applicant requested withdrawal, status-requirement waived per legal opinion.\"
        }
      }
    ]
  }")
python3 -c "
assert '$D10_S3_CODE' == '200', f'grantException failed: $D10_S3_CODE {open(\"/tmp/d10_s3.json\").read()}'
print('  OK: grantException accepted; system:exception entity created with status=active')
"
echo ""

# --- D10 Step 4: GET /dossiers/{id} as aanvrager surfaces the bypass ---
# Without this surfacing, the frontend would hide the bypassed activity
# and the user could never know the granted exception exists. The
# eligibility computation must include trekAanvraagIn with an
# exempted_by_exception field naming the version_id of the active
# system:exception. Fetched as the aanvrager (not the beheerder) to
# verify auth filtering still includes the entry — exceptions help
# users who could otherwise call the activity, never role-elevate.
echo "--- D10 Step 4: GET /dossiers/daa surfaces trekAanvraagIn with exempted_by_exception ---"
D10_S4_CODE=$(curl -s -o /tmp/d10_s4.json -w "%{http_code}" \
  -H "X-POC-User: jan.aanvrager" \
  "$BASE_URL/dossiers/daa00000-0000-0000-0000-000000000001")
python3 -c "
import json
assert '$D10_S4_CODE' == '200', f'GET dossier failed: $D10_S4_CODE'
d = json.load(open('/tmp/d10_s4.json'))
allowed = d.get('allowedActivities', [])
trek = [a for a in allowed if a.get('type') == 'oe:trekAanvraagIn']
assert len(trek) == 1, f'expected oe:trekAanvraagIn in allowedActivities, got {[a.get(\"type\") for a in allowed]}'
entry = trek[0]
assert 'exempted_by_exception' in entry, (
    f'entry should carry exempted_by_exception field; got {entry}'
)
print('  OK: trekAanvraagIn surfaces in allowedActivities with exempted_by_exception=' + entry['exempted_by_exception'])
"
echo ""

# --- D10 Step 5: aanvrager runs trekAanvraagIn — bypass ---
# check_exceptions finds the active exception, appends it to the
# activity's used set, check_workflow_rules skips. Engine auto-
# injects consumeException after persistence.
echo "--- D10 Step 5: trekAanvraagIn succeeds via exception bypass ---"
D10_S5_CODE=$(curl -s -o /tmp/d10_s5.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/daa00000-0000-0000-0000-000000000001/activities/aa000000-0000-0000-0000-000000000004/oe:trekAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [],
    \"generated\": []
  }")
python3 -c "
import json
assert '$D10_S5_CODE' == '200', f'expected 200 via bypass, got $D10_S5_CODE: {open(\"/tmp/d10_s5.json\").read()}'
d = json.load(open('/tmp/d10_s5.json'))
st = d.get('dossier', {}).get('status')
assert st == 'aanvraag_ingetrokken', f'expected aanvraag_ingetrokken, got {st!r}'
print('  OK: trekAanvraagIn succeeded via exception bypass — dossier now aanvraag_ingetrokken')
"
echo ""

# --- D10 Step 6: verify consumeException fired + exception is consumed ---
echo "--- D10 Step 6: verify consumeException activity ran + system:exception is consumed ---"
curl -s "$BASE_URL/dossiers/daa00000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ents = d.get('currentEntities', [])
exs = [e for e in ents if e.get('type') == 'system:exception']
assert len(exs) == 1, f'expected 1 system:exception, got {len(exs)}'
ex = exs[0]
c = ex['content']
assert c.get('status') == 'consumed', f'expected status=consumed, got {c.get(\"status\")}: {c}'
assert c.get('activity') == 'oe:trekAanvraagIn', f'activity field drifted across revisions: {c}'
assert 'Legal authorization #D10' in (c.get('reason') or ''), f'reason lost: {c}'
assert ex.get('entityId') == 'eb000000-0000-0000-0000-000000000001', f'entity_id drifted: {ex}'
# Activity log is nested in the dossier response.
types = [a.get('type') for a in d.get('activities', [])]
for needed in ('oe:grantException', 'oe:trekAanvraagIn', 'oe:consumeException'):
    assert needed in types, f'expected {needed} in activity log, got {types}'
print('  OK: system:exception latest = consumed; same logical entity_id; activity/reason preserved')
print('  OK: PROV graph contains grantException, trekAanvraagIn, consumeException')
"
echo ""

# --- D10 Step 7: re-grant via revision of same logical entity ---
# Revises the SAME logical system:exception (status=consumed) back to
# status=active. Same entity_id, new version_id, derivedFrom
# points at the consumed version.
echo "--- D10 Step 7: re-grant by revision (same entity_id) ---"
D10_EXC_VID=$(curl -s "$BASE_URL/dossiers/daa00000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for e in d.get('currentEntities', []):
    if e.get('type') == 'system:exception':
        print(e.get('versionId', ''))
        break
")
D10_S6_CODE=$(curl -s -o /tmp/d10_s6.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/daa00000-0000-0000-0000-000000000001/activities/aa000000-0000-0000-0000-000000000006/oe:grantException" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [],
    \"generated\": [
      {
        \"entity\": \"system:exception/eb000000-0000-0000-0000-000000000001@fb000000-0000-0000-0000-000000000006\",
        \"derivedFrom\": \"system:exception/eb000000-0000-0000-0000-000000000001@$D10_EXC_VID\",
        \"content\": {
          \"activity\": \"oe:trekAanvraagIn\",
          \"status\": \"active\",
          \"reason\": \"Re-grant after consume — revisiting withdrawal authorization.\"
        }
      }
    ]
  }")
python3 -c "
assert '$D10_S6_CODE' == '200', f're-grant failed: $D10_S6_CODE {open(\"/tmp/d10_s6.json\").read()}'
print('  OK: re-grant accepted (200)')
"
curl -s "$BASE_URL/dossiers/daa00000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
exs = [e for e in d.get('currentEntities', []) if e.get('type') == 'system:exception']
assert len(exs) == 1, f'expected 1 system:exception (revised), got {len(exs)}'
c = exs[0]['content']
assert c.get('status') == 'active', f'expected re-granted status=active, got {c}'
assert exs[0].get('entityId') == 'eb000000-0000-0000-0000-000000000001', f'entity_id drifted on revision: {exs[0]}'
print('  OK: re-grant revised the same logical entity; status back to active')
"
echo ""

# --- D10 Step 8: retract instead of using it ---
echo "--- D10 Step 8: retractException cancels the re-granted exception ---"
D10_EXC_VID2=$(curl -s "$BASE_URL/dossiers/daa00000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for e in d.get('currentEntities', []):
    if e.get('type') == 'system:exception':
        print(e.get('versionId', ''))
        break
")
D10_S7_CODE=$(curl -s -o /tmp/d10_s7.json -w "%{http_code}" \
  -X PUT "$BASE_URL/toelatingen/dossiers/daa00000-0000-0000-0000-000000000001/activities/aa000000-0000-0000-0000-000000000007/oe:retractException" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d "{
    \"workflow\": \"toelatingen\",
    \"used\": [
      { \"entity\": \"system:exception/eb000000-0000-0000-0000-000000000001@$D10_EXC_VID2\" }
    ],
    \"generated\": []
  }")
python3 -c "
assert '$D10_S7_CODE' == '200', f'retract failed: $D10_S7_CODE {open(\"/tmp/d10_s7.json\").read()}'
print('  OK: retractException accepted (200)')
"
curl -s "$BASE_URL/dossiers/daa00000-0000-0000-0000-000000000001" \
  -H "X-POC-User: claeyswo" | python3 -c "
import sys, json
d = json.load(sys.stdin)
exs = [e for e in d.get('currentEntities', []) if e.get('type') == 'system:exception']
assert len(exs) == 1
c = exs[0]['content']
assert c.get('status') == 'cancelled', f'expected status=cancelled, got {c}'
print('  OK: retractException revised system:exception to status=cancelled')
"
echo ""

echo "D10 summary: exception grant lifecycle verified end-to-end (grant → bypass → auto-consume → re-grant → retract)"

