#!/bin/bash
# ============================================================================
# Test requests for the Toelatingen POC
#
# Prerequisites:
# 1. Update poc_users in workflow.yaml (see below)
# 2. Delete dossiers.db to start fresh
# 3. Start the server: uvicorn main:app --reload
# 4. Run this script: bash test_requests.sh
#
# POC Users needed (add to workflow.yaml poc_users section):
#
#  - id: "aaa00000-0000-0000-0000-000000000001"
#    username: "jan.aanvrager"
#    type: "persoon"
#    name: "Jan Peeters"
#    roles: ["85010100123"]
#    properties:
#      rrn: "85010100123"
#
#  - id: "aaa00000-0000-0000-0000-000000000002"
#    username: "firma.acme"
#    type: "persoon"
#    name: "ACME BV"
#    roles: ["kbo-toevoeger:0123456789"]
#    properties:
#      kbo: "0123456789"
#
#  - id: "bbb00000-0000-0000-0000-000000000001"
#    username: "marie.brugge"
#    type: "medewerker"
#    name: "Marie Vandenbroeck"
#    roles: ["behandelaar", "beslisser", "gemeente-toevoeger:https://data.vlaanderen.be/id/organisatie/brugge"]
#    properties:
#      gemeente: "Brugge"
#
#  - id: "bbb00000-0000-0000-0000-000000000002"
#    username: "benjamma"
#    type: "medewerker"
#    name: "Ben Jansen"
#    roles: ["behandelaar", "beslisser", "gemeente-toevoeger:https://data.vlaanderen.be/id/organisatie/oe"]
#    properties:
#      organisatie: "oe"
#
# ============================================================================

BASE_URL="http://localhost:8000"

echo "============================================"
echo "DOSSIER 1: Brugge, RRN aanvrager"
echo "Flow: dienAanvraagIn → neemBeslissing(onvolledig) → vervolledigAanvraag → neemBeslissing(goedgekeurd)"
echo "============================================"
echo ""

# ============================================================================
# D1 Step 1: dienAanvraagIn (jan.aanvrager)
# ============================================================================
echo "--- D1 Step 1: dienAanvraagIn ---"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "type": "dienAanvraagIn",
    "workflow": "toelatingen",
    "role": "oe:aanvrager",
    "used": [
      {
        "entity": "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Restauratie gevelbekleding stadhuis",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/10001"
        }
      },
      {
        "entity": "https://id.erfgoed.net/erfgoedobjecten/10001"
      }
    ]
  }' | python3 -m json.tool
echo ""

# ============================================================================
# D1 Step 2: neemBeslissing — direct call (marie.brugge)
# Sends beslissing (onvolledig) + handtekening (getekend: true) together
# Handler determines status → "aanvraag_onvolledig"
# ============================================================================
echo "--- D1 Step 2: neemBeslissing (onvolledig, direct call) ---"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000002" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "type": "neemBeslissing",
    "role": "oe:verantwoordelijke_organisatie",
    "used": [
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
        "content": {
          "getekend": true
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

# Check status — should be "aanvraag_onvolledig"
echo "--- D1 Check status (expect: aanvraag_onvolledig) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: jan.aanvrager" | python3 -m json.tool
echo ""

# ============================================================================
# D1 Step 3: vervolledigAanvraag (jan.aanvrager)
# ============================================================================
echo "--- D1 Step 3: vervolledigAanvraag ---"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000004" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "type": "vervolledigAanvraag",
    "role": "oe:aanvrager",
    "used": [
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
      },
      {
        "entity": "https://id.erfgoed.net/erfgoedobjecten/10001"
      }
    ]
  }' | python3 -m json.tool
echo ""

# ============================================================================
# D1 Step 4: neemBeslissing — direct call (marie.brugge) → goedgekeurd
# New versions of beslissing + handtekening
# ============================================================================
echo "--- D1 Step 4: neemBeslissing (goedgekeurd, direct call) ---"
curl -s -X PUT "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/activities/a1000000-0000-0000-0000-000000000005" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: marie.brugge" \
  -d '{
    "type": "neemBeslissing",
    "role": "oe:verantwoordelijke_organisatie",
    "used": [
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
        "content": {
          "getekend": true
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

# Check final status — should be "toelating_verleend"
echo "--- D1 Final status (expect: toelating_verleend) ---"
curl -s "$BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: jan.aanvrager" | python3 -m json.tool
echo ""

echo "D1 PROV Graph: $BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/prov/graph"
echo ""
echo ""


echo "============================================"
echo "DOSSIER 2: Gent (not Brugge), KBO aanvrager"
echo "Flow: dienAanvraagIn → doeVoorstelBeslissing(onvolledig) → tekenBeslissing → vervolledigAanvraag → bewerkAanvraag → doeVoorstelBeslissing(goedgekeurd) → tekenBeslissing"
echo "============================================"
echo ""

# ============================================================================
# D2 Step 1: dienAanvraagIn (firma.acme)
# ============================================================================
echo "--- D2 Step 1: dienAanvraagIn ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: firma.acme" \
  -d '{
    "type": "dienAanvraagIn",
    "workflow": "toelatingen",
    "role": "oe:aanvrager",
    "used": [
      {
        "entity": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000001",
        "content": {
          "onderwerp": "Plaatsing zonnepanelen op beschermd pand",
          "handeling": "plaatsing",
          "aanvrager": { "kbo": "0123456789" },
          "gemeente": "Gent",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001"
        }
      },
      {
        "entity": "https://id.erfgoed.net/erfgoedobjecten/20001"
      }
    ]
  }' | python3 -m json.tool
echo ""

# ============================================================================
# D2 Step 2: doeVoorstelBeslissing — onvolledig (benjamma)
# ============================================================================
echo "--- D2 Step 2: doeVoorstelBeslissing (onvolledig) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000002" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "type": "doeVoorstelBeslissing",
    "role": "oe:behandelaar",
    "used": [
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

# ============================================================================
# D2 Step 3: tekenBeslissing — approved (benjamma)
# Triggers neemBeslissing as side effect → status becomes "aanvraag_onvolledig"
# ============================================================================
echo "--- D2 Step 3: tekenBeslissing (approved, triggers neemBeslissing) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000003" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "type": "tekenBeslissing",
    "role": "oe:ondertekenaar",
    "used": [
      {
        "entity": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000003",
        "content": {
          "getekend": true
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

# Check status — should be "aanvraag_onvolledig"
echo "--- D2 Check status (expect: aanvraag_onvolledig) ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: firma.acme" | python3 -m json.tool
echo ""

# ============================================================================
# D2 Step 4: vervolledigAanvraag (firma.acme)
# ============================================================================
echo "--- D2 Step 4: vervolledigAanvraag ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000004" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: firma.acme" \
  -d '{
    "type": "vervolledigAanvraag",
    "role": "oe:aanvrager",
    "used": [
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
      },
      {
        "entity": "https://id.erfgoed.net/erfgoedobjecten/20001"
      }
    ]
  }' | python3 -m json.tool
echo ""

# ============================================================================
# D2 Step 5: bewerkAanvraag (benjamma, behandelaar)
# ============================================================================
echo "--- D2 Step 5: bewerkAanvraag ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000005" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "type": "bewerkAanvraag",
    "role": "oe:behandelaar",
    "used": [
      {
        "entity": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000005",
        "derivedFrom": "oe:aanvraag/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000004",
        "content": {
          "onderwerp": "Plaatsing zonnepanelen op beschermd pand - met technische fiche en advies behandelaar",
          "handeling": "plaatsing",
          "aanvrager": { "kbo": "0123456789" },
          "gemeente": "Gent",
          "object": "https://id.erfgoed.net/erfgoedobjecten/20001"
        }
      },
      {
        "entity": "https://id.erfgoed.net/erfgoedobjecten/20001"
      }
    ]
  }' | python3 -m json.tool
echo ""

# ============================================================================
# D2 Step 6: doeVoorstelBeslissing — goedgekeurd (benjamma)
# New version of beslissing
# ============================================================================
echo "--- D2 Step 6: doeVoorstelBeslissing (goedgekeurd) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000006" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "type": "doeVoorstelBeslissing",
    "role": "oe:behandelaar",
    "used": [
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

# ============================================================================
# D2 Step 7: tekenBeslissing — approved (benjamma)
# New version of handtekening, triggers neemBeslissing → "toelating_verleend"
# ============================================================================
echo "--- D2 Step 7: tekenBeslissing (approved, triggers neemBeslissing) ---"
curl -s -X PUT "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/activities/a2000000-0000-0000-0000-000000000007" \
  -H "Content-Type: application/json" \
  -H "X-POC-User: benjamma" \
  -d '{
    "type": "tekenBeslissing",
    "role": "oe:ondertekenaar",
    "used": [
      {
        "entity": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000007",
        "derivedFrom": "oe:handtekening/e2000000-0000-0000-0000-000000000003@f2000000-0000-0000-0000-000000000003",
        "content": {
          "getekend": true
        }
      }
    ]
  }' | python3 -m json.tool
echo ""

# Check final status — should be "toelating_verleend"
echo "--- D2 Final status (expect: toelating_verleend) ---"
curl -s "$BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001" \
  -H "X-POC-User: firma.acme" | python3 -m json.tool
echo ""

echo "D2 PROV Graph: $BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/prov/graph"
echo ""
echo ""

echo "============================================"
echo "List all dossiers (beheerder view)"
echo "============================================"
curl -s "$BASE_URL/dossiers" \
  -H "X-POC-User: claeyswo" | python3 -m json.tool
echo ""

echo "============================================"
echo "PROV Graph URLs:"
echo "  D1: $BASE_URL/dossiers/d1000000-0000-0000-0000-000000000001/prov/graph"
echo "  D2: $BASE_URL/dossiers/d2000000-0000-0000-0000-000000000001/prov/graph"
echo "(Open in browser with X-POC-User header, e.g. via ModHeader extension)"
echo "============================================"
