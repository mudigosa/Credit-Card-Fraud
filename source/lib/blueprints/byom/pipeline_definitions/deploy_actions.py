# #####################################################################################################################
#  Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.                                            #
#                                                                                                                     #
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance     #
#  with the License. A copy of the License is located at                                                              #
#                                                                                                                     #
#  http://www.apache.org/licenses/LICENSE-2.0                                                                         #
#                                                                                                                     #
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES  #
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions     #
#  and limitations under the License.                                                                                 #
# #####################################################################################################################
import uuid
from aws_cdk import (
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_codepipeline_actions as codepipeline_actions,
    aws_cloudformation as cloudformation,
    core,
)
from lib.blueprints.byom.pipeline_definitions.helpers import (
    codepipeline_policy,
    suppress_lambda_policies,
    suppress_pipeline_policy,
    add_logs_policy,
)
from lib.conditional_resource import ConditionalResources
from lib.blueprints.byom.pipeline_definitions.iam_policies import (
    create_service_role,
    sagemaker_baseline_job_policy,
    sagemaker_logs_metrics_policy_document,
    batch_transform_policy,
    s3_policy_write,
    s3_policy_read,
    cloudformation_stackset_policy,
    cloudformation_stackset_instances_policy,
    kms_policy_document,
)


lambda_service = "lambda.amazonaws.com"
lambda_handler = "main.handler"


def sagemaker_layer(scope, blueprint_bucket):
    """
    sagemaker_layer creates a Lambda layer with Sagemaker SDK installed in it to allow Lambda functions call
    Sagemaker SDK's methods such as create_model(), etc.

    :blueprint_bucket: CDK object of the blueprint bucket that contains resources for BYOM pipeline
    :scope: CDK Construct scope that's needed to create CDK resources
    :return: Lambda layer version in a form of a CDK object
    """
    # Lambda sagemaker layer for sagemaker sdk that is used in create sagemaker model step
    return lambda_.LayerVersion(
        scope,
        "sagemakerlayer",
        code=lambda_.Code.from_bucket(blueprint_bucket, "blueprints/byom/lambdas/sagemaker_layer.zip"),
        compatible_runtimes=[lambda_.Runtime.PYTHON_3_8],
    )


def batch_transform(
    scope,  # NOSONAR:S107 this function is designed to take many arguments
    id,
    blueprint_bucket,
    assets_bucket,
    model_name,
    inference_instance,
    batch_input_bucket,
    batch_inference_data,
    batch_job_output_location,
    kms_key_arn,
    sm_layer,
):
    """
    batch_transform creates a sagemaker batch transform job in a lambda

    :scope: CDK Construct scope that's needed to create CDK resources
    :blueprint_bucket: CDK object of the blueprint bucket that contains resources for BYOM pipeline
    :assets_bucket: the bucket cdk object where pipeline assets are stored
    :model_name: name of the sagemaker model to be created, in the form of a CDK CfnParameter object
    :inference_instance: compute instance type for the sagemaker inference endpoint, in the form of
    a CDK CfnParameter object
    :batch_input_bucket: bucket name where the batch data is stored
    :batch_inference_data: location of the batch inference data in assets bucket, in the form of
    a CDK CfnParameter object
    :batch_job_output_location: S3 bucket location where the result of the batch job will be stored
    :kms_key_arn: optionl kmsKeyArn used to encrypt job's output and instance volume.
    :sm_layer: sagemaker lambda layer
    :return: Lambda function
    """
    s3_read = s3_policy_read(
        list(
            set(
                [
                    f"arn:aws:s3:::{assets_bucket.bucket_name}",
                    f"arn:aws:s3:::{assets_bucket.bucket_name}/*",
                    f"arn:aws:s3:::{batch_input_bucket}",
                    f"arn:aws:s3:::{batch_inference_data}",
                ]
            )
        )
    )
    s3_write = s3_policy_write(
        [
            f"arn:aws:s3:::{batch_job_output_location}/*",
        ]
    )

    batch_transform_permissions = batch_transform_policy()

    lambda_role = create_service_role(
        scope,
        "batch_transform_lambda_role",
        "lambda.amazonaws.com",
        (
            "Role that creates a lambda function assumes to create a sagemaker batch transform "
            "job in the aws mlops pipeline."
        ),
    )

    lambda_role.add_to_policy(batch_transform_permissions)
    lambda_role.add_to_policy(s3_read)
    lambda_role.add_to_policy(s3_write)
    add_logs_policy(lambda_role)

    batch_transform_lambda = lambda_.Function(
        scope,
        id,
        runtime=lambda_.Runtime.PYTHON_3_8,
        handler="main.handler",
        layers=[sm_layer],
        role=lambda_role,
        code=lambda_.Code.from_bucket(blueprint_bucket, "blueprints/byom/lambdas/batch_transform.zip"),
        environment={
            "model_name": model_name,
            "inference_instance": inference_instance,
            "assets_bucket": assets_bucket.bucket_name,
            "batch_inference_data": batch_inference_data,
            "batch_job_output_location": batch_job_output_location,
            "kms_key_arn": kms_key_arn,
            "LOG_LEVEL": "INFO",
        },
    )

    batch_transform_lambda.node.default_child.cfn_options.metadata = suppress_lambda_policies()

    return batch_transform_lambda


def create_data_baseline_job(
    scope,  # NOSONAR:S107 this function is designed to take many arguments
    blueprint_bucket,
    assets_bucket,
    baseline_job_name,
    training_data_location,
    baseline_job_output_location,
    endpoint_name,
    instance_type,
    instance_volume_size,
    max_runtime_seconds,
    kms_key_arn,
    kms_key_arn_provided_condition,
    stack_name,
):
    """
    create_baseline_job creates a data baseline processing job in a lambda invoked codepipeline action

    :scope: CDK Construct scope that's needed to create CDK resources
    :blueprint_bucket: CDK object of the blueprint bucket that contains resources for BYOM pipeline
    :assets_bucket: the bucket cdk object where pipeline assets are stored
    :baseline_job_name: name of the baseline job to be created
    :training_data_location: location of the training data used to train the deployed model
    :baseline_job_output_location: S3 prefix in the S3 assets bucket to store the output of the job
    :endpoint_name: name of the deployed SageMaker endpoint to be monitored
    :instance_type: compute instance type for the baseline job, in the form of a CDK CfnParameter object
    :instance_volume_size: volume size of the EC2 instance
    :max_runtime_seconds: max time the job is allowd to run
    :kms_key_arn: kms key arn to encrypt the baseline job's output
    :stack_name: model monitor stack name
    :return: codepipeline action in a form of a CDK object that can be attached to a codepipeline stage
    """
    s3_read = s3_policy_read(
        [
            f"arn:aws:s3:::{assets_bucket.bucket_name}",
            f"arn:aws:s3:::{assets_bucket.bucket_name}/{training_data_location}",
        ]
    )
    s3_write = s3_policy_write(
        [
            f"arn:aws:s3:::{baseline_job_output_location}/*",
        ]
    )

    create_baseline_job_policy = sagemaker_baseline_job_policy(baseline_job_name)
    sagemaker_logs_policy = sagemaker_logs_metrics_policy_document(scope, "BaselineLogsMetrcis")

    # Kms Key permissions
    kms_policy = kms_policy_document(scope, "BaselineKmsPolicy", kms_key_arn)
    # add conditions to KMS and ECR policies
    core.Aspects.of(kms_policy).add(ConditionalResources(kms_key_arn_provided_condition))

    # create sagemaker role
    sagemaker_role = create_service_role(
        scope,
        "create_baseline_sagemaker_role",
        "sagemaker.amazonaws.com",
        "Role that is create sagemaker model Lambda function assumes to create a baseline job.",
    )
    # attach the conditional policies
    kms_policy.attach_to_role(sagemaker_role)

    # create a trust relation to assume the Role
    sagemaker_role.add_to_policy(iam.PolicyStatement(actions=["sts:AssumeRole"], resources=[sagemaker_role.role_arn]))
    # creating a role so that this lambda can create a baseline job
    lambda_role = create_service_role(
        scope,
        "create_baseline_job_lambda_role",
        lambda_service,
        "Role that is create_data_baseline_job Lambda function assumes to create a baseline job in the pipeline.",
    )

    sagemaker_logs_policy.attach_to_role(sagemaker_role)
    sagemaker_role.add_to_policy(create_baseline_job_policy)
    sagemaker_role.add_to_policy(s3_read)
    sagemaker_role.add_to_policy(s3_write)
    sagemaker_role_nodes = sagemaker_role.node.find_all()
    sagemaker_role_nodes[2].node.default_child.cfn_options.metadata = suppress_pipeline_policy()
    lambda_role.add_to_policy(iam.PolicyStatement(actions=["iam:PassRole"], resources=[sagemaker_role.role_arn]))
    lambda_role.add_to_policy(create_baseline_job_policy)
    lambda_role.add_to_policy(s3_write)
    lambda_role.add_to_policy(s3_read)
    add_logs_policy(lambda_role)

    # defining the lambda function that gets invoked in this stage
    create_baseline_job_lambda = lambda_.Function(
        scope,
        "create_data_baseline_job",
        runtime=lambda_.Runtime.PYTHON_3_8,
        handler=lambda_handler,
        role=lambda_role,
        code=lambda_.Code.from_bucket(blueprint_bucket, "blueprints/byom/lambdas/create_data_baseline_job.zip"),
        environment={
            "BASELINE_JOB_NAME": baseline_job_name,
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            "SAGEMAKER_ENDPOINT_NAME": endpoint_name,
            "TRAINING_DATA_LOCATION": training_data_location,
            "BASELINE_JOB_OUTPUT_LOCATION": baseline_job_output_location,
            "INSTANCE_TYPE": instance_type,
            "INSTANCE_VOLUME_SIZE": instance_volume_size,
            "MAX_RUNTIME_SECONDS": max_runtime_seconds,
            "ROLE_ARN": sagemaker_role.role_arn,
            "KMS_KEY_ARN": kms_key_arn,
            "STACK_NAME": stack_name,
            "LOG_LEVEL": "INFO",
        },
        timeout=core.Duration.minutes(10),
    )

    create_baseline_job_lambda.node.default_child.cfn_options.metadata = suppress_lambda_policies()
    role_child_nodes = create_baseline_job_lambda.role.node.find_all()
    role_child_nodes[2].node.default_child.cfn_options.metadata = suppress_pipeline_policy()

    return create_baseline_job_lambda


def create_stackset_action(
    scope,  # NOSONAR:S107 this function is designed to take many arguments
    action_name,
    blueprint_bucket,
    source_output,
    artifact,
    template_file,
    stage_params_file,
    accound_ids,
    org_ids,
    regions,
    assets_bucket,
    stack_name,
):
    """
    create_stackset_action an invokeLambda action to be added to AWS Codepipeline stage

    :scope: CDK Construct scope that's needed to create CDK resources
    :action_name: name of the StackSet action
    :blueprint_bucket: CDK object of the blueprint bucket that contains resources for BYOM pipeline
    :source_output: CDK object of the Source action's output
    :artifact: name of the input aritifcat to the StackSet action
    :template_file: name of the Cloudformation template to be deployed
    :stage_params_file: name of the template parameters for the satge
    :accound_ids: list of AWS acounts where the stack with be deployed
    :org_ids: list of AWS orginizational ids where the stack with be deployed
    :regions: list of regions where the stack with be deployed
    :assets_bucket: the bucket cdk object where pipeline assets are stored
    :stack_name: name of the stack to be deployed
    :return: codepipeline invokeLambda action in a form of a CDK object that can be attached to a codepipeline stage
    """
    # creating a role so that this lambda can create a baseline job
    lambda_role = create_service_role(
        scope,
        f"{action_name}_role",
        lambda_service,
        "The role that is assumed by create_update_cf_stackset Lambda function.",
    )
    # make the stackset name unique
    stack_name = f"{stack_name}-{str(uuid.uuid4())[:8]}"
    # cloudformation stackset permissions
    cloudformation_stackset_permissions = cloudformation_stackset_policy(stack_name)
    cloudformation_stackset_instances_permissions = cloudformation_stackset_instances_policy(stack_name)

    lambda_role.add_to_policy(cloudformation_stackset_permissions)
    lambda_role.add_to_policy(cloudformation_stackset_instances_permissions)
    add_logs_policy(lambda_role)

    # defining the lambda function that gets invoked in this stage
    create_update_cf_stackset_lambda = lambda_.Function(
        scope,
        f"{action_name}_stackset_lambda",
        runtime=lambda_.Runtime.PYTHON_3_8,
        handler="main.lambda_handler",
        role=lambda_role,
        code=lambda_.Code.from_bucket(blueprint_bucket, "blueprints/byom/lambdas/create_update_cf_stackset.zip"),
        timeout=core.Duration.minutes(15),
    )

    create_update_cf_stackset_lambda.node.default_child.cfn_options.metadata = suppress_lambda_policies()
    role_child_nodes = create_update_cf_stackset_lambda.role.node.find_all()
    role_child_nodes[2].node.default_child.cfn_options.metadata = suppress_pipeline_policy()

    # Create codepipeline action
    create_stackset_action = codepipeline_actions.LambdaInvokeAction(
        action_name=action_name,
        inputs=[source_output],
        variables_namespace=f"{action_name}-namespace",
        lambda_=create_update_cf_stackset_lambda,
        user_parameters={
            "stackset_name": stack_name,
            "artifact": artifact,
            "template_file": template_file,
            "stage_params_file": stage_params_file,
            "accound_ids": accound_ids,
            "org_ids": org_ids,
            "regions": regions,
        },
        run_order=1,
    )
    return (create_update_cf_stackset_lambda.function_arn, create_stackset_action)


def create_cloudformation_action(
    scope, action_name, stack_name, source_output, template_file, template_parameters_file, run_order=1
):
    """
    create_cloudformation_actio a CloudFormation action to be added to AWS Codepipeline stage

    :scope: CDK Construct scope that's needed to create CDK resources
    :action_name: name of the StackSet action
    :stack_name: name of the stack to be deployed
    :source_output: CDK object of the Source action's output
    :template_file: name of the Cloudformation template to be deployed
    :template_parameters_file: name of the template parameters
    :return: codepipeline CloudFormation action in a form of a CDK object that can be attached to a codepipeline stage
    """

    # Create codepipeline's cloudformation action
    create_cloudformation_action = codepipeline_actions.CloudFormationCreateUpdateStackAction(
        action_name=action_name,
        stack_name=stack_name,
        capabilities=[cloudformation.CloudFormationCapabilities.NAMED_IAM],
        template_path=source_output.at_path(template_file),
        # Admin permissions are added to the deployement role used by the CF action for simplicity
        # and deploy different resources by different MLOps pipelines. Roles are defined by the
        # pipelines' cloudformation templates.
        admin_permissions=True,
        template_configuration=source_output.at_path(template_parameters_file),
        variables_namespace=f"{action_name}-namespace",
        replace_on_failure=True,
        run_order=run_order,
    )

    return create_cloudformation_action


def create_invoke_lambda_custom_resource(
    scope,  # NOSONAR:S107 this function is designed to take many arguments
    id,
    lambda_function_arn,
    lambda_function_name,
    blueprint_bucket,
    custom_resource_properties,
):
    """
    create_invoke_lambda_custom_resource creates a custom resource to invoke lambda function

    :scope: CDK Construct scope that's needed to create CDK resources
    :id: the logicalId of teh CDK resource
    :lambda_function_arn: arn of the lambda function to be invoked (str)
    :lambda_function_name: name of the lambda function to be invoked (str)
    :blueprint_bucket: CDK object of the blueprint bucket that contains resources for BYOM pipeline
    :custom_resource_properties: user provided properties (dict)

    :return: CDK Custom Resource
    """
    custom_resource_lambda_fn = lambda_.Function(
        scope,
        id,
        code=lambda_.Code.from_bucket(blueprint_bucket, "blueprints/byom/lambdas/invoke_lambda_custom_resource.zip"),
        handler="index.handler",
        runtime=lambda_.Runtime.PYTHON_3_8,
        timeout=core.Duration.minutes(5),
    )

    custom_resource_lambda_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=[
                "lambda:InvokeFunction",
            ],
            resources=[lambda_function_arn],
        )
    )
    custom_resource_lambda_fn.node.default_child.cfn_options.metadata = suppress_lambda_policies()

    invoke_lambda_custom_resource = core.CustomResource(
        scope,
        f"{id}CustomeResource",
        service_token=custom_resource_lambda_fn.function_arn,
        properties={
            "function_name": lambda_function_name,
            "message": f"Invoking lambda function: {lambda_function_name}",
            **custom_resource_properties,
        },
        resource_type="Custom::InvokeLambda",
    )

    return invoke_lambda_custom_resource