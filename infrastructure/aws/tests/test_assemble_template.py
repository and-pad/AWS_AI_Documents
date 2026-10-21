import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assemble_template import assemble_template


class AssembleTemplateTests(unittest.TestCase):
    def test_modules_compile_to_one_root_stack_with_existing_logical_ids(self):
        template = assemble_template()
        self.assertIn("Resources:\n  # auth", template)
        self.assertNotIn("Modules:", template)
        self.assertNotIn("AWS::Serverless::Application", template)
        self.assertNotIn("AWS::CloudFormation::Stack", template)

        resource_section = template.split("Resources:\n", 1)[1].split("\nOutputs:\n", 1)[0]
        logical_ids = re.findall(r"(?m)^  ([A-Za-z][A-Za-z0-9]*):\s*$", resource_section)
        self.assertEqual(len(logical_ids), 35)
        self.assertEqual(len(logical_ids), len(set(logical_ids)))
        self.assertTrue(
            {
                "DocumentsBucket",
                "ResultsBucket",
                "JobsTable",
                "DocumentQueue",
                "DocumentDeadLetterQueue",
                "UserPool",
                "UserPoolClient",
                "OllamaApiTokenSecret",
                "DocumentProcessorRepository",
                "ProcessorVpc",
                "JobsApiFunction",
                "DocumentProcessorFunction",
                "ProcessorTaskRole",
            }.issubset(logical_ids)
        )

    def test_public_stack_outputs_and_parameter_names_remain_in_root(self):
        template = assemble_template()
        for public_name in (
            "DocumentsBucketName",
            "ResultsBucketName",
            "DocumentQueueArn",
            "DocumentQueueUrl",
            "JobsTableName",
            "JobsApiUrl",
            "CognitoUserPoolId",
            "CognitoUserPoolClientId",
            "DocumentProcessorRepositoryUri",
            "DocumentProcessorPrivateUrl",
            "OllamaApiTokenSecretArn",
            "ProcessorVpcId",
        ):
            self.assertRegex(template, rf"(?m)^  {public_name}:")
        for parameter in (
            "Environment",
            "ProcessorDeployment",
            "DocumentProcessorDesiredCount",
            "ProcessorMaxConcurrentJobs",
            "OllamaBaseUrl",
            "OllamaIPv4Cidr",
            "QueueVisibilityTimeout",
            "FrontendOrigin",
            "MaxUserJobs",
        ):
            self.assertRegex(template, rf"(?m)^  {parameter}:")

    def test_production_frontend_origin_is_shared_by_api_and_lambda_cors(self):
        template = assemble_template()
        self.assertRegex(
            template,
            r"(?ms)^  FrontendOrigin:\n    Type: String\n    Default: https://dfw9n3vcykvho\.cloudfront\.net",
        )
        self.assertIn("AllowOrigin: !Sub \"'${FrontendOrigin}'\"", template)
        self.assertIn("CORS_ORIGIN: !Ref FrontendOrigin", template)
        self.assertIn("AllowHeaders: \"'Content-Type,Authorization'\"", template)
        self.assertIn("AllowMethods: \"'GET,POST,DELETE,OPTIONS'\"", template)
        self.assertIn("AddDefaultAuthorizerToCorsPreflight: false", template)
        self.assertIn("Access-Control-Allow-Origin: !Sub \"'${FrontendOrigin}'\"", template)
        self.assertNotIn("Access-Control-Allow-Origin: '*'", template)

    def test_results_lifecycle_only_expires_temporary_checkpoints(self):
        template = assemble_template()
        results = template.split("  ResultsBucket:\n", 1)[1].split("\n  JobsTable:\n", 1)[0]
        self.assertIn("Prefix: checkpoints/", results)
        self.assertIn("ExpirationInDays: 30", results)
        self.assertNotIn("Prefix: results/", results)
        self.assertNotIn("ExpirationInDays", results.split("Tags:", 1)[-1])

    def test_checkpoint_iam_is_scoped_to_private_checkpoint_prefix(self):
        template = assemble_template()
        processor_role = template.split("  ProcessorTaskRole:\n", 1)[1].split("\n  OllamaApiTokenSecret:\n", 1)[0]
        self.assertIn('"${ResultsBucket.Arn}/checkpoints/*"', processor_role)
        self.assertIn("s3:ListBucket", processor_role)
        self.assertIn("s3:prefix: checkpoints/*", processor_role)
        api = template.split("  JobsApiFunction:\n", 1)[1]
        self.assertIn('"${ResultsBucket.Arn}/checkpoints/*"', api)
        self.assertRegex(
            api,
            r"(?s)s3:ListBucket\s+Resource: !GetAtt ResultsBucket\.Arn\s+Condition:\s+StringLike:\s+s3:prefix:\s+- results/\*\s+- checkpoints/\*",
        )

    def test_checkpoint_change_preserves_queue_and_ollama_timeouts(self):
        template = assemble_template()
        self.assertRegex(template, r"(?ms)^  QueueVisibilityTimeout:\n    Type: Number\n    Default: 900")
        self.assertRegex(template, r"(?ms)^  SqsVisibilityHeartbeatSeconds:\n    Type: Number\n    Default: 300")
        self.assertRegex(template, r"(?ms)^  OllamaReadTimeout:\n    Type: Number\n    Default: 1800")
        self.assertRegex(template, r"(?ms)^  OllamaPageNumPredict:\n    Type: Number\n    Default: 6000\n    MinValue: 1")
        self.assertRegex(template, r"(?ms)^  OllamaSynthesisNumPredict:\n    Type: Number\n    Default: 2000\n    MinValue: 1")
        processor = template.split("  ProcessorTaskDefinition:\n", 1)[1]
        self.assertIn("Name: OLLAMA_PAGE_NUM_PREDICT\n              Value: !Ref OllamaPageNumPredict", processor)
        self.assertIn("Name: OLLAMA_SYNTHESIS_NUM_PREDICT\n              Value: !Ref OllamaSynthesisNumPredict", processor)
        self.assertIn("Name: OLLAMA_NUM_CTX\n              Value: !Ref OllamaNumCtx", processor)
        self.assertIn("maxReceiveCount: 3", template)

    def test_processor_concurrency_is_configurable_without_changing_task_count(self):
        template = assemble_template()
        self.assertRegex(
            template,
            r"(?ms)^  ProcessorMaxConcurrentJobs:\n    Type: Number\n    Default: 1\n    MinValue: 1\n    MaxValue: 2",
        )
        self.assertIn(
            "Name: PROCESSOR_MAX_CONCURRENT_JOBS\n              Value: !Ref ProcessorMaxConcurrentJobs",
            template,
        )
        self.assertIn("Cpu: '256'", template)
        self.assertIn("Memory: '1024'", template)


if __name__ == "__main__":
    unittest.main()
