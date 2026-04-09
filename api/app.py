# app.py
import os, uuid, time, json
from flask import Flask, request, jsonify
import boto3
from botocore.exceptions import ClientError

app = Flask(__name__)

API_KEY = os.getenv("OCR_API_KEY", "")
S3_BUCKET = os.getenv("OCR_S3_BUCKET")
SQS_URL = os.getenv("OCR_SQS_URL")
DDB_TABLE = os.getenv("OCR_DDB_TABLE")
AWS_REGION = os.getenv("AWS_REGION")

session = boto3.session.Session(region_name=AWS_REGION)
s3 = session.client("s3")
sqs = session.client("sqs")
ddb = session.client("dynamodb")

def now_ms(): return int(time.time() * 1000)

def require_api_key(req):
    # ถ้าไม่ได้ตั้ง API_KEY ให้ปิดระบบไปเลย
    if not API_KEY:
        return False
    return req.headers.get("x-api-key", "") == API_KEY

def validate_file(f):
    allowed = {"application/pdf", "image/png", "image/jpeg"}
    if f.mimetype not in allowed:
        return False, f"unsupported content-type: {f.mimetype}"
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 25 * 1024 * 1024:
        return False, "file too large (max 25MB)"
    return True, None

@app.post("/jobs")
def create_job():
    if not require_api_key(request):
        return jsonify({"error": "unauthorized"}), 401

    if not (S3_BUCKET and SQS_URL and DDB_TABLE and AWS_REGION):
        return jsonify({"error": "server misconfigured (missing env)"}), 500

    if "file" not in request.files:
        return jsonify({"error": "file is required (multipart field: file)"}), 400

    f = request.files["file"]
    ok, err = validate_file(f)
    if not ok:
        return jsonify({"error": err}), 400

    job_id = str(uuid.uuid4())
    filename = (f.filename or "upload").replace("/", "_")
    input_key = f"uploads/{job_id}/{filename}"
    result_key = f"results/{job_id}/result.json"
    ts = now_ms()

    # 1) upload to S3
    try:
        s3.upload_fileobj(f, S3_BUCKET, input_key)
    except ClientError as e:
        return jsonify({"error": "s3 upload failed", "detail": str(e)}), 500

    # 2) put DynamoDB (QUEUED)
    try:
        ddb.put_item(
            TableName=DDB_TABLE,
            Item={
                "job_id": {"S": job_id},
                "status": {"S": "QUEUED"},
                "input_s3_key": {"S": input_key},
                "result_s3_key": {"S": result_key},
                "original_filename": {"S": filename},
                "content_type": {"S": f.mimetype},
                "created_at": {"N": str(ts)},
                "updated_at": {"N": str(ts)},
            },
            ConditionExpression="attribute_not_exists(job_id)"
        )
    except ClientError as e:
        # cleanup: ลบไฟล์ที่อัปขึ้น S3 ไปแล้ว
        try:
            s3.delete_object(Bucket=S3_BUCKET, Key=input_key)
        except Exception:
            pass
        return jsonify({"error": "ddb put failed", "detail": str(e)}), 500

    # 3) send SQS
    msg = {"job_id": job_id, "bucket": S3_BUCKET, "input_key": input_key, "result_key": result_key, "created_at": ts}
    try:
        sqs.send_message(QueueUrl=SQS_URL, MessageBody=json.dumps(msg))
    except ClientError as e:
        # mark FAILED เพราะไม่เข้าคิว
        ddb.update_item(
            TableName=DDB_TABLE,
            Key={"job_id": {"S": job_id}},
            UpdateExpression="SET #s=:s, error_message=:e, updated_at=:u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": "FAILED"},
                ":e": {"S": f"sqs send failed: {e}"},
                ":u": {"N": str(now_ms())},
            }
        )
        return jsonify({"error": "sqs send failed", "job_id": job_id}), 500

    return jsonify({"job_id": job_id, "status": "QUEUED"}), 201

@app.get("/jobs/<job_id>")
def get_job(job_id):
    if not require_api_key(request):
        return jsonify({"error": "unauthorized"}), 401

    resp = ddb.get_item(TableName=DDB_TABLE, Key={"job_id": {"S": job_id}})
    if "Item" not in resp:
        return jsonify({"error": "not found"}), 404

    it = resp["Item"]
    out = {
        "job_id": job_id,
        "status": it["status"]["S"],
        "input_s3_key": it.get("input_s3_key", {}).get("S"),
        "result_s3_key": it.get("result_s3_key", {}).get("S"),
        "error_message": it.get("error_message", {}).get("S"),
        "updated_at": int(it.get("updated_at", {}).get("N", "0")),
    }

    if out["status"] == "DONE" and out["result_s3_key"]:
        out["result_download_url"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": out["result_s3_key"]},
            ExpiresIn=3600
        )
    return jsonify(out), 200

@app.get("/health")
def health():
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

