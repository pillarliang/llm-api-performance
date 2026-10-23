"""Create this project's private bucket using OSS_* environment credentials."""
import os
import uuid

import alibabacloud_oss_v2 as oss

cfg = oss.config.load_default()
cfg.region = os.environ["OSS_REGION"]
cfg.endpoint = os.environ.get("OSS_ENDPOINT") or None
cfg.credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
client = oss.Client(cfg)
bucket = os.environ["OSS_BUCKET"]
client.put_bucket(oss.PutBucketRequest(bucket=bucket, acl="private"))
key = ".storage-check/" + uuid.uuid4().hex
payload = b"llm-performance OSS read/write verification"
try:
    client.put_object(oss.PutObjectRequest(bucket=bucket, key=key, body=payload))
    response = client.get_object(oss.GetObjectRequest(bucket=bucket, key=key))
    with response.body as body:
        assert b"".join(body.iter_bytes()) == payload
finally:
    client.delete_object(oss.DeleteObjectRequest(bucket=bucket, key=key))
print(f"Verified private bucket: oss://{bucket} ({cfg.region})")
