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
            "OllamaBaseUrl",
            "OllamaIPv4Cidr",
            "QueueVisibilityTimeout",
            "FrontendOrigin",
            "MaxUserJobs",
        ):
            self.assertRegex(template, rf"(?m)^  {parameter}:")


if __name__ == "__main__":
    unittest.main()
