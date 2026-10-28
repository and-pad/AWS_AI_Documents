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
        self.assertEqual(len(logical_ids), 45)
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
                "QuestionQueue",
                "QuestionDeadLetterQueue",
                "QuestionTable",
                "QuestionProcessorFunction",
                "QuestionDlqReconcilerFunction",
                "QuestionDlqReconcilerRole",
                "QuestionDlqReconcilerLogGroup",
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
            "OllamaModel",
            "OllamaAskModel",
            "OllamaAskNumPredict",
            "OllamaAskNumCtx",
            "OllamaAskEvidenceNumPredict",
            "OllamaAskReadTimeout",
            "OllamaCoordinatorQueueWaitSeconds",
            "OllamaCoordinatorRetryDelaySeconds",
            "QuestionQueueVisibilityTimeout",
            "QuestionRetryDelaySeconds",
            "MaxUserQuestionsPerDay",
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
        self.assertIn("Name: OLLAMA_COORDINATOR_QUEUE_WAIT_SECONDS\n              Value: !Ref OllamaCoordinatorQueueWaitSeconds", processor)
        self.assertIn("maxReceiveCount: 3", template)

    def test_visual_and_question_models_have_independent_settings(self):
        template = assemble_template()
        self.assertRegex(template, r"(?ms)^  OllamaModel:\n    Type: String\n    Default: qwen3-vl:8b-instruct")
        self.assertRegex(template, r"(?ms)^  OllamaAskModel:\n    Type: String\n    Default: qwen3-vl:8b-instruct")
        self.assertRegex(template, r"(?ms)^  OllamaAskNumPredict:\n    Type: Number\n    Default: 2000")
        self.assertRegex(template, r"(?ms)^  OllamaAskNumCtx:\n    Type: Number\n    Default: 8192")
        self.assertIn("Name: OLLAMA_MODEL\n              Value: !Ref OllamaModel", template)
        self.assertIn("OLLAMA_ASK_MODEL: !Ref OllamaAskModel", template)
        self.assertIn("OLLAMA_ASK_NUM_CTX: !Ref OllamaAskNumCtx", template)
        self.assertNotIn("OLLAMA_ASK_CONTEXT_MAX_CHARS", template)

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

    def test_questions_have_separate_authorized_async_path_and_short_lived_storage(self):
        template = assemble_template()
        self.assertIn("Path: /jobs/{job_id}/ask", template)
        self.assertIn("Path: /jobs/{job_id}/asks/{ask_id}", template)
        self.assertGreaterEqual(template.count("Authorizer: CognitoAuthorizer"), 6)
        self.assertIn("Queue: !GetAtt QuestionQueue.Arn", template)
        self.assertIn("VisibilityTimeout: !Ref QuestionQueueVisibilityTimeout", template)
        question_worker = re.search(
            r"(?ms)^  QuestionProcessorFunction:\n(.*?)(?=^  QuestionDlqReconcilerRole:)",
            template,
        ).group(1)
        question_dlq_worker = template.split("  QuestionDlqReconcilerFunction:\n", 1)[1]
        for worker in (question_worker, question_dlq_worker):
            self.assertNotIn("ReservedConcurrentExecutions:", worker)
            self.assertIn("BatchSize: 1", worker)
            self.assertIn("MaximumConcurrency: 2", worker)
            self.assertIn("ReportBatchItemFailures", worker)
        self.assertIn("Handler: app.question_worker.lambda_dlq_handler", template)
        self.assertIn("Handler: app.question_worker.lambda_handler", template)
        self.assertIn("VisibilityTimeout: 180", template)
        self.assertIn("QUESTION_RETRY_DELAY_SECONDS: !Ref QuestionRetryDelaySeconds", template)
        self.assertIn("QUESTION_DLQ_URL: !Ref QuestionDeadLetterQueue", template)
        self.assertRegex(template, r"(?ms)^  OllamaAskReadTimeout:\n    Type: Number\n    Default: 780\n    MinValue: 10\n    MaxValue: 780")
        self.assertRegex(template, r"(?ms)^  QuestionQueueVisibilityTimeout:\n    Type: Number\n    Default: 5400\n    MinValue: 5400")
        question_table = template.split("  QuestionTable:", 1)[1].split("\n  ", 1)[0]
        self.assertIn("AttributeName: expires_at", template)
        self.assertIn("Enabled: true", template)
        self.assertIn("MaxUserQuestionsPerDay", template)
        self.assertNotIn("DocumentQueueVisibilityTimeout", template)

    def test_jobs_api_can_execute_question_transaction_with_table_scoped_permissions(self):
        template = assemble_template()
        jobs_api = re.search(
            r"(?ms)^  JobsApiFunction:\n(.*?)(?=^  QuestionProcessorRole:)",
            template,
        ).group(1)

        self.assertRegex(
            jobs_api,
            r"(?s)Action:\n\s+- dynamodb:GetItem\n\s+- dynamodb:PutItem\n"
            r"\s+- dynamodb:UpdateItem\n\s+- dynamodb:DeleteItem\n"
            r"\s+- dynamodb:TransactWriteItems\n\s+- dynamodb:ConditionCheckItem\n"
            r"\s+Resource: !GetAtt JobsTable.Arn",
        )
        self.assertRegex(
            jobs_api,
            r"(?s)Action:\n\s+- dynamodb:GetItem\n\s+- dynamodb:PutItem\n"
            r"\s+- dynamodb:UpdateItem\n\s+- dynamodb:DeleteItem\n"
            r"\s+- dynamodb:Query\n\s+- dynamodb:TransactWriteItems\n"
            r"\s+Resource: !GetAtt QuestionTable.Arn",
        )
        self.assertNotIn("dynamodb:*", jobs_api)

    def test_question_worker_can_condition_check_only_the_parent_jobs_table(self):
        template = assemble_template()
        processor_role = re.search(
            r"(?ms)^  QuestionProcessorRole:\n(.*?)(?=^  QuestionProcessorFunction:)",
            template,
        ).group(1)
        self.assertRegex(
            processor_role,
            r"(?s)Action: dynamodb:ConditionCheckItem\n\s+Resource: !GetAtt JobsTable.Arn",
        )
        self.assertNotRegex(
            processor_role,
            r"(?s)Action: dynamodb:ConditionCheckItem\n\s+Resource:.*QuestionTable",
        )
        self.assertNotIn("dynamodb:*", processor_role)


if __name__ == "__main__":
    unittest.main()
