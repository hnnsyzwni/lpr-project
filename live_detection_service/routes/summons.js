const express = require("express");
const axios = require("axios");
const xml2js = require("xml2js");
const path = require("path");
const fs = require("fs");

const router = express.Router();
const API_URL = "https://prm.citycarpark.my/CCP_ArchService/MessageGateway.svc";
const SOAP_ACTION = "http://www.citycarpark.my/MessageGatewayService/ProcessMessage";

let requestInProgress = {}; // Store ongoing requests

// --- helpers ---
const normPlate = (v = "") => v.trim().toUpperCase().replace(/\s+/g, "");

const buildSoap = (vehicleNumber) => `<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Header>
    <RequestCode>REQ_11</RequestCode>
    <AgencyID>VISTAAPP</AgencyID>
    <AgencyKey>HISV2024@APP</AgencyKey>
  </s:Header>
  <s:Body>
    <Request>
      <OffenderIDNo></OffenderIDNo>
      <VehicleRegistrationNumber>${vehicleNumber}</VehicleRegistrationNumber>
      <NoticeNo></NoticeNo>
    </Request>
  </s:Body>
</s:Envelope>`;

// Map any gateway "unpaid/open" codes to Unpaid
const isUnpaid = (raw) => {
  const v = String(raw || "").trim().toUpperCase();
  return ["T", "U", "UNPAID", "O", "OPEN", "N"].includes(v);
};

async function fetchSummons(vehicleNumber) {
  const xml = buildSoap(vehicleNumber);
  const resp = await axios.post(API_URL, xml, {
    headers: { "Content-Type": "text/xml; charset=utf-8", SOAPAction: SOAP_ACTION },
    timeout: 15000,
  });

  return new Promise((resolve, reject) => {
    xml2js.parseString(resp.data, (err, result) => {
      if (err) return reject(err);
      try {
        const body = result["s:Envelope"]["s:Body"][0]["Response"][0];
        const list = body["Summonses"]?.[0]["Summons"] || [];
        const formatted = list.map((s) => {
          const statusRaw = (s.NoticeStatus?.[0] || "").trim().toUpperCase();
          return {
            plate: (s.VehicleRegistrationNo?.[0] || "UNKNOWN").trim().toUpperCase(),
            noticeNo: s.NoticeNo?.[0]?.trim() || "UNKNOWN",
            offence: s.OffenceSection?.[0]?.trim() || "",
            location: s.OffenceLocation?.[0]?.trim() || "",
            date: s.OffenceDate?.[0]?.trim() || "",
            statusRaw,
            status: isUnpaid(statusRaw) ? "Unpaid" : "Paid",
            amount: s.Amount?.[0] ? parseFloat(s.Amount[0]) : 0,
            due_date: s.DueDate?.[0]?.trim() || "",
          };
        });
        resolve(formatted);
      } catch (e) {
        reject(e);
      }
    });
  });
}

// ============== ROUTES ==============

// NEW: GET /summons?plate=ABC1234 (used by your HTML)
router.get("/", async (req, res) => {
  const vehicleNumber = normPlate(req.query.plate || req.query.vehicleNumber || "");
  if (!vehicleNumber) return res.status(400).json({ error: "Vehicle number is required" });

  if (requestInProgress[vehicleNumber]) {
    console.warn(`⚠️ Duplicate GET for ${vehicleNumber}. Skipping...`);
    return res.status(429).json({ error: "Request already in progress. Try again later." });
  }

  requestInProgress[vehicleNumber] = true;
  console.log(`🔎 GET summons for: ${vehicleNumber}`);

  try {
    const all = await fetchSummons(vehicleNumber);
    const unpaid = all.filter((s) => s.status === "Unpaid");
    console.log("➡️ Returning", unpaid.length ? "unpaid only" : "all (for debug)", "items");
    delete requestInProgress[vehicleNumber];
    // If nothing matches unpaid, return all so you can see statusRaw and adjust mapping if needed
    return res.json(unpaid.length ? unpaid : all);
  } catch (e) {
    delete requestInProgress[vehicleNumber];
    console.error("❌ GET fetch error:", e.message);
    return res.status(500).json({ error: "Failed to fetch summons data." });
  }
});

// Existing: POST / (body: { vehicleNumber })
router.post("/", async (req, res) => {
  const vehicleNumber = normPlate(req.body.vehicleNumber || "");
  if (!vehicleNumber) return res.status(400).json({ error: "Vehicle number is required" });

  if (requestInProgress[vehicleNumber]) {
    console.warn(`⚠️ Duplicate POST for ${vehicleNumber}. Skipping...`);
    return res.status(429).json({ error: "Request already in progress. Try again later." });
  }

  requestInProgress[vehicleNumber] = true;
  console.log(`✅ Requesting summons for plate: ${vehicleNumber}`);

  try {
    const all = await fetchSummons(vehicleNumber);
    const unpaid = all.filter((s) => s.status === "Unpaid");
    console.log("🚀 Processed Summons:", unpaid);
    delete requestInProgress[vehicleNumber];
    return res.json(unpaid);
  } catch (error) {
    delete requestInProgress[vehicleNumber];
    console.error("❌ API Request Failed:", error.message);
    return res.status(500).json({ error: "Failed to fetch summons data." });
  }
});

// Your existing PDF route (kept as-is; still using sample data)
router.get("/download-pdf", async (req, res) => {
  const { plate } = req.query;
  if (!plate) return res.status(400).json({ error: "Vehicle number is required" });

  console.log(`📥 Generating PDF for plate: ${plate}`);

  // TODO: replace this hardcoded list with `await fetchSummons(normPlate(plate))`
  const summonsList = [
    { noticeNo: "KN0802400002", plate: "DCL12", offence: "PERINTAH 30(c)", location: "JALAN BUKIT SETONGKOL 7", date: "2024-12-31", status: "Unpaid", amount: 300 },
    { noticeNo: "KN0742400189", plate: "DCL12", offence: "PERINTAH 4", location: "JALAN MAT KILAU", date: "2024-12-24", status: "Unpaid", amount: 300 },
  ];

  const pdfPath = path.join(__dirname, `summons_${plate}.pdf`);
  try {
    await generateSummonsPDF(plate, summonsList, pdfPath);
    res.download(pdfPath, `summons_${plate}.pdf`, (err) => {
      if (err) {
        console.error("❌ Error sending PDF:", err);
        res.status(500).json({ error: "Failed to generate PDF" });
      }
      setTimeout(() => {
        fs.unlinkSync(pdfPath);
        console.log(`🗑️ Deleted: ${pdfPath}`);
      }, 5000);
    });
  } catch (error) {
    console.error("❌ PDF Generation Error:", error);
    res.status(500).json({ error: "Failed to generate PDF" });
  }
});

module.exports = router;
