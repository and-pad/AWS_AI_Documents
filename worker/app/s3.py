import boto3


def download_object(bucket: str, key: str) -> bytes:
    response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    return response["Body"].read()
