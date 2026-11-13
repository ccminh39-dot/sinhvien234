import os
import sys
import json
import base64
import tempfile
import urllib.parse
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from web_server import WorkflowEngine, DataIngestionEngine, AuditLogger, ExpansionDecisionAgent

def handle_workflow_payload(payload):
    action = payload.get("action", "step_validate")
    sheet_url = payload.get("sheet_url", "")
    source_type = payload.get("source_type", "sheets")
    preset_name = payload.get("preset_name", "standard")
    feedback = payload.get("feedback", "")
    force_verify_ok = payload.get("force_verify_ok", False)

    if action == "step_validate":
        raw_data = None
        if source_type == "sheets":
            validation = WorkflowEngine.step_validate_sheets(sheet_url)
            if not validation.get("ok"):
                return validation
            raw_data = validation.get("raw_data")
        elif source_type == "preset":
            preset_file = {
                "standard": os.path.join(BASE_DIR, "data", "dataset_standard.json"),
                "missing": os.path.join(BASE_DIR, "data", "dataset_missing_info.json"),
                "boundary": os.path.join(BASE_DIR, "data", "dataset_boundary_case.json")
            }.get(preset_name, os.path.join(BASE_DIR, "data", "dataset_standard.json"))
            with open(preset_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        elif source_type == "csv_content":
            csv_text = payload.get("content", "")
            try:
                raw_data = DataIngestionEngine.parse_csv_string(csv_text)
            except Exception as e:
                return {"ok": False, "step": "Sheets", "error": f"Báo lỗi định dạng bảng: {str(e)}"}
        elif source_type == "xlsx_base64":
            b64_data = payload.get("content_base64", "")
            temp_xlsx = os.path.join(tempfile.gettempdir(), "temp_upload.xlsx")
            with open(temp_xlsx, "wb") as f:
                f.write(base64.b64decode(b64_data))
            try:
                raw_data = DataIngestionEngine.parse_xlsx_file(temp_xlsx)
            except Exception as e:
                return {"ok": False, "step": "Sheets", "error": f"Báo lỗi file Excel: {str(e)}"}

        if not raw_data or not raw_data.get("territories"):
            return {"ok": False, "step": "Sheets", "error": "Báo lỗi: Không tìm thấy dữ liệu hợp lệ. Yêu cầu nhập đúng link Google Sheets hoặc file theo cấu trúc."}

        commentary = WorkflowEngine.generate_commentary_step1(raw_data)
        return {
            "ok": True,
            "step": "Sheets",
            "message": f"Nạp thành công {len(raw_data.get('territories', []))} địa bàn!",
            "raw_data": raw_data,
            "commentary": commentary
        }

    elif action == "step_cleaning":
        raw_data = payload.get("raw_data")
        if not raw_data:
            return {"ok": False, "step": "Cleaning", "error": "Thiếu dữ liệu gốc từ Bước 1 để thực hiện Cleaning."}
        return WorkflowEngine.step_cleaning(raw_data)

    elif action == "step_process":
        cleaned_data = payload.get("cleaned_data")
        if not cleaned_data:
            return {"ok": False, "step": "Process", "error": "Thiếu dữ liệu làm sạch từ Bước 2 để xử lý nghiệp vụ."}
        return WorkflowEngine.step_process(cleaned_data, feedback=feedback)

    elif action == "step_verify":
        process_res = payload.get("process_res")
        if not process_res:
            return {"ok": False, "step": "Verify", "error": "Thiếu kết quả tính toán từ Bước 3 để thẩm định."}
        if force_verify_ok:
            commentary = WorkflowEngine.generate_commentary_step4(process_res.get("results", {}), False, [])
            commentary["summary"] = "Chuyên viên đã chủ động phê duyệt ngoại lệ sau khi xem xét các cảnh báo (HITL Approved)."
            return {
                "ok": True,
                "step": "Verify",
                "status": "verified_ok",
                "message": "Phê duyệt ngoại lệ thành công! Sẵn sàng xuất HTML Dashboard.",
                "commentary": commentary
            }
        return WorkflowEngine.step_verify(process_res)

    elif action == "step_export" or action == "run_pipeline":
        process_res = payload.get("process_res")
        if not process_res:
            raw_data = payload.get("raw_data")
            if not raw_data:
                return {"ok": False, "step": "Export_Dashboard", "error": "Thiếu dữ liệu để xuất Dashboard."}
            c_res = WorkflowEngine.step_cleaning(raw_data)
            process_res = WorkflowEngine.step_process(c_res["cleaned_data"], feedback=feedback)

        results = process_res["results"]
        report_markdown = process_res["report_markdown"]
        audit_events = process_res["audit_events"]

        return {
            "ok": True,
            "step": "Export_Dashboard",
            "status": "completed",
            "message": "Toàn bộ quy trình hoàn tất! Đã xuất HTML Dashboard và Báo cáo đề xuất.",
            "territories": [
                {
                    "id": r["territory"]["id"],
                    "name": r["territory"]["name"],
                    "facts": r["verified_facts"],
                    "fit": r["fit_analysis"],
                    "score": r["score_res"],
                    "is_selected": r in results["selected_territories"]
                }
                for r in results["pipeline_results"]
            ],
            "selected_top_2": [
                {
                    "id": s["territory"]["id"],
                    "name": s["territory"]["name"],
                    "score": s["score_res"]["total_score"],
                    "reasons": s["fit_analysis"]["feasibility_assessment"]
                }
                for s in results["selected_territories"]
            ],
            "report_markdown": report_markdown,
            "audit_events": audit_events
        }

    return {"ok": False, "error": f"Hành động không hợp lệ: {action}"}


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")

    # XỬ LÝ GET
    if method == "GET":
        if path in ("/", "/index.html"):
            index_path = os.path.join(BASE_DIR, "ui", "index.html")
            with open(index_path, "r", encoding="utf-8") as f:
                body = f.read().encode("utf-8")
            start_response("200 OK", [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body)))
            ])
            return [body]

        elif path.startswith("/assets/"):
            file_name = path.replace("/assets/", "")
            file_path = os.path.join(BASE_DIR, "ui", "assets", file_name)
            if os.path.exists(file_path):
                ct = "image/jpeg" if file_name.endswith((".jpg", ".jpeg")) else ("image/png" if file_name.endswith(".png") else "application/octet-stream")
                with open(file_path, "rb") as f:
                    body = f.read()
                start_response("200 OK", [
                    ("Content-Type", ct),
                    ("Content-Length", str(len(body)))
                ])
                return [body]
            else:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"Asset Not Found"]

        elif path == "/download_sample_csv":
            csv_path = os.path.join(BASE_DIR, "data", "file_mau_dung_dinh_dang_hungphat.csv")
            with open(csv_path, "rb") as f:
                body = f.read()
            start_response("200 OK", [
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", "attachment; filename=file_mau_dung_dinh_dang_hungphat.csv"),
                ("Content-Length", str(len(body)))
            ])
            return [body]

        elif path == "/download_sample_xlsx":
            xlsx_path = os.path.join(BASE_DIR, "data", "file_mau_dung_dinh_dang_hungphat.xlsx")
            with open(xlsx_path, "rb") as f:
                body = f.read()
            start_response("200 OK", [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", "attachment; filename=file_mau_dung_dinh_dang_hungphat.xlsx"),
                ("Content-Length", str(len(body)))
            ])
            return [body]

        elif path.startswith("/api/load_preset"):
            qs = environ.get("QUERY_STRING", "")
            query = urllib.parse.parse_qs(qs)
            preset_name = query.get("name", ["standard"])[0]
            file_map = {
                "standard": os.path.join(BASE_DIR, "data", "dataset_standard.json"),
                "missing": os.path.join(BASE_DIR, "data", "dataset_missing_info.json"),
                "boundary": os.path.join(BASE_DIR, "data", "dataset_boundary_case.json")
            }
            target_file = file_map.get(preset_name, os.path.join(BASE_DIR, "data", "dataset_standard.json"))
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            resp = json.dumps({"status": "success", "data": data}, ensure_ascii=False).encode("utf-8")
            start_response("200 OK", [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(resp)))
            ])
            return [resp]

        else:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not Found"]

    # XỬ LÝ POST
    elif method == "POST":
        try:
            content_length = int(environ.get("CONTENT_LENGTH") or 0)
            body = environ["wsgi.input"].read(content_length).decode("utf-8") if content_length > 0 else "{}"
            payload = json.loads(body)
        except Exception as e:
            payload = {}

        if path == "/api/workflow_execute":
            res_dict = handle_workflow_payload(payload)
            resp = json.dumps(res_dict, ensure_ascii=False).encode("utf-8")
            start_response("200 OK", [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(resp)))
            ])
            return [resp]

        elif path == "/api/run":
            dataset_name = payload.get("dataset", "standard")
            file_map = {
                "standard": os.path.join(BASE_DIR, "data", "dataset_standard.json"),
                "missing": os.path.join(BASE_DIR, "data", "dataset_missing_info.json"),
                "boundary": os.path.join(BASE_DIR, "data", "dataset_boundary_case.json")
            }
            target_file = file_map.get(dataset_name, os.path.join(BASE_DIR, "data", "dataset_standard.json"))
            logger = AuditLogger()
            agent = ExpansionDecisionAgent(data_file_path=target_file, logger=logger)
            results = agent.run_pipeline()
            report_markdown = agent.generate_management_report(results)

            res_dict = {
                "status": "success",
                "dataset": dataset_name,
                "territories": [
                    {
                        "id": r["territory"]["id"],
                        "name": r["territory"]["name"],
                        "facts": r["verified_facts"],
                        "fit": r["fit_analysis"],
                        "score": r["score_res"],
                        "is_selected": r in results["selected_territories"]
                    }
                    for r in results["pipeline_results"]
                ],
                "selected_top_2": [
                    {
                        "id": s["territory"]["id"],
                        "name": s["territory"]["name"],
                        "score": s["score_res"]["total_score"],
                        "reasons": s["fit_analysis"]["feasibility_assessment"]
                    }
                    for s in results["selected_territories"]
                ],
                "report_markdown": report_markdown,
                "audit_events": logger.get_events()
            }
            resp = json.dumps(res_dict, ensure_ascii=False).encode("utf-8")
            start_response("200 OK", [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(resp)))
            ])
            return [resp]

        else:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not Found"]

    start_response("405 Method Not Allowed", [("Content-Type", "text/plain")])
    return [b"Method Not Allowed"]

application = app

