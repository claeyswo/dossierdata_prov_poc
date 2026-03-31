#!/bin/bash
# ============================================================================
# Test requests for the Toelatingen POC
# Uses typed endpoints — no type or role needed in payload
# ============================================================================

BASE_URL="http://localhost:8000"

echo "============================================"
echo "DOSSIER 1: Brugge, RRN aanvrager"
echo "============================================"
echo ""

echo "--- D1 Step 1: dienAanvraagIn ---"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000001/dienAanvraagIn" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "workflow": "toelatingen",
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/10001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Restauratie gevelbekleding stadhuis",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/10001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D1 Step 2: neemBeslissing (onvolledig, direct) ---"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000002/neemBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "generated": [
      {
        "entity": "oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000002",
        "content": {
          "beslissing": "onvolledig",
          "datum": "2026-03-26T10:00:00Z",
          "object": "https://id.erfgoed.net/erfgoedobjecten/10001",
          "brief": "https://dms.example.com/brieven/d1-brief-001"
        }
      },
      {
        "entity": "oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000003",
        "content": { "getekend": true }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D1 Check status (expect: aanvraag_onvolledig) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "--- D1 Step 3: vervolledigAanvraag ---"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000004/vervolledigAanvraag" \
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
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000005/neemBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "generated": [
      {
        "entity": "oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000005",
        "derivedFrom": "oe:beslissing/e1000000-0000-0000-0000-000000000002@f1000000-0000-0000-0000-000000000002",
        "content": {
          "beslissing": "goedgekeurd",
          "datum": "2026-03-27T14:00:00Z",
          "object": "https://id.erfgoed.net/erfgoedobjecten/10001",
          "brief": "https://dms.example.com/brieven/d1-brief-002"
        }
      },
      {
        "entity": "oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000006",
        "derivedFrom": "oe:handtekening/e1000000-0000-0000-0000-000000000003@f1000000-0000-0000-0000-000000000003",
        "content": { "getekend": true }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D1 Final status (expect: toelating_verleend) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D1 Graph: $BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/prov/graph"
echo ""
echo ""


echo "============================================"
echo "DOSSIER 2: Gent, KBO aanvrager, separate signer, declined signing"
echo "  behandelaar: benjamma"
echo "  ondertekenaar: sophie.tekent"
echo "============================================"
echo ""

echo "--- D2 Step 1: dienAanvraagIn (firma.acme) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000001/dienAanvraagIn" \
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
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000002/doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "generated": [
      {
        "entity": "oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000002",
        "content": {
          "beslissing": "onvolledig",
          "datum": "2026-03-26T11:00:00Z",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001",
          "brief": "https://dms.example.com/brieven/d2-brief-001"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 3: tekenBeslissing — sophie signs (triggers neemBeslissing → onvolledig) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000003/tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
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
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000004/vervolledigAanvraag" \
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
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000005/bewerkAanvraag" \
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
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000006/doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "generated": [
      {
        "entity": "oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000006",
        "derivedFrom": "oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000002",
        "content": {
          "beslissing": "goedgekeurd",
          "datum": "2026-03-28T09:00:00Z",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001",
          "brief": "https://dms.example.com/brieven/d2-brief-002"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 7: tekenBeslissing — sophie DECLINES (getekend: false → klaar_voor_behandeling) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000007/tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
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
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000008/doeVoorstelBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "generated": [
      {
        "entity": "oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000008",
        "derivedFrom": "oe:beslissing/e2000000-0000-0000-0000-000000000002@f2000000-0000-0000-0000-000000000006",
        "content": {
          "beslissing": "goedgekeurd",
          "datum": "2026-03-29T10:00:00Z",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001",
          "brief": "https://dms.example.com/brieven/d2-brief-003"
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D2 Step 9: tekenBeslissing — sophie SIGNS (triggers neemBeslissing → goedgekeurd) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000009/tekenBeslissing" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: sophie.tekent" \
  -d '{
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

echo "D2 Graph: $BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/prov/graph"
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
curl -s -X PUT "$BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001/activities/a3000000-0000-0000-0000-000000000001/dienAanvraagIn" \
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
curl -s -X PUT "$BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001/activities" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "workflow": "toelatingen",
    "activities": [
      {
        "activity_id": "a3000000-0000-0000-0000-000000000002",
        "type": "bewerkAanvraag",
        "used": [
          { "entity": "https://id.erfgoed.net/erfgoedobjecten/30001" }
        ],
        "generated": [
          {
            "entity": "oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000002",
            "derivedFrom": "oe:aanvraag/e3000000-0000-0000-0000-000000000001@f3000000-0000-0000-0000-000000000001",
            "content": {
              "onderwerp": "Batch test — renovatie kapel (bewerkt met advies)",
              "handeling": "renovatie",
              "aanvrager": { "rrn": "85010100123" },
              "gemeente": "Brugge",
              "object": "https://id.erfgoed.net/erfgoedobjecten/30001"
            }
          }
        ]
      },
      {
        "activity_id": "a3000000-0000-0000-0000-000000000003",
        "type": "doeVoorstelBeslissing",
        "generated": [
          {
            "entity": "oe:beslissing/e3000000-0000-0000-0000-000000000002@f3000000-0000-0000-0000-000000000003",
            "content": {
              "beslissing": "goedgekeurd",
              "datum": "2026-03-30T12:00:00Z",
              "object": "https://id.erfgoed.net/erfgoedobjecten/30001",
              "brief": "https://dms.example.com/brieven/d3-brief-001"
            }
          }
        ]
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D3 Final status (expect: beslissing_te_tekenen) ---"
curl -s "$BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D3 Graph: $BASE_URL/dossiers/d3000000-0000-0000-0000-000000000001/prov/graph"
echo ""
echo ""

echo "============================================"
echo "DOSSIER 4: Batch — explicit used ref between activities"
echo "  bewerkAanvraag generates oe:aanvraag@new_version"
echo "  doeVoorstelBeslissing explicitly uses that version"
echo "============================================"
echo ""

echo "--- D4 Step 1: dienAanvraagIn ---"
curl -s -X PUT "$BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001/activities/a4000000-0000-0000-0000-000000000001/dienAanvraagIn" \
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
curl -s -X PUT "$BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001/activities" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "workflow": "toelatingen",
    "activities": [
      {
        "activity_id": "a4000000-0000-0000-0000-000000000002",
        "type": "bewerkAanvraag",
        "used": [
          { "entity": "https://id.erfgoed.net/erfgoedobjecten/40001" }
        ],
        "generated": [
          {
            "entity": "oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000002",
            "derivedFrom": "oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000001",
            "content": {
              "onderwerp": "Explicit ref batch test — restauratie toren (bewerkt)",
              "handeling": "renovatie",
              "aanvrager": { "rrn": "85010100123" },
              "gemeente": "Brugge",
              "object": "https://id.erfgoed.net/erfgoedobjecten/40001"
            }
          }
        ]
      },
      {
        "activity_id": "a4000000-0000-0000-0000-000000000003",
        "type": "doeVoorstelBeslissing",
        "used": [
          { "entity": "oe:aanvraag/e4000000-0000-0000-0000-000000000001@f4000000-0000-0000-0000-000000000002" }
        ],
        "generated": [
          {
            "entity": "oe:beslissing/e4000000-0000-0000-0000-000000000002@f4000000-0000-0000-0000-000000000003",
            "content": {
              "beslissing": "goedgekeurd",
              "datum": "2026-03-30T14:00:00Z",
              "object": "https://id.erfgoed.net/erfgoedobjecten/40001",
              "brief": "https://dms.example.com/brieven/d4-brief-001"
            }
          }
        ]
      }
    ]
  }' | python3 -m json.tool
echo ""

echo "--- D4 Final status (expect: beslissing_te_tekenen) ---"
curl -s "$BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: marie.brugge" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Status: {d[\"status\"]}')"
echo ""

echo "D4 Graph: $BASE_URL/dossiers/d4000000-0000-0000-0000-000000000001/prov/graph"
