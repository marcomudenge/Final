import logging
from typing import Any, Dict, List, Optional
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

class EC2InstanceWrapper:
    """Encapsulates Amazon Elastic Compute Cloud (Amazon EC2) instance actions using the client interface."""

    def __init__(
        self, ec2_client: Any, instances: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """
        Initializes the EC2InstanceWrapper with an EC2 client and optional instances.

        :param ec2_client: A Boto3 Amazon EC2 client. This client provides low-level
                           access to AWS EC2 services.
        :param instances: A list of dictionaries representing Boto3 Instance objects. These are high-level objects that
                          wrap instance actions.
        """
        self.ec2_client = ec2_client
        self.instances = instances or []

    @classmethod
    def from_client(cls) -> "EC2InstanceWrapper":
        """
        Creates an EC2InstanceWrapper instance with a default EC2 client.

        :return: An instance of EC2InstanceWrapper initialized with the default EC2 client.
        """
        ec2_client = boto3.client("ec2",
                                  region_name=os.environ.get("AWS_DEFAULT_REGION", "us-west-1"),
                                  aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                                  aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                                  aws_session_token=os.environ.get("AWS_SESSION_TOKEN")
                                  )
        return cls(ec2_client)

    def create(
        self,
        image_id: str,
        instance_type: str,
        key_pair_name: str,
        security_group_ids: Optional[List[str]] = None,
        instance_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Creates a new EC2 instance in the default VPC of the current account.

        The instance starts immediately after it is created.

        :param image_id: The ID of the Amazon Machine Image (AMI) to use for the instance.
        :param instance_type: The type of instance to create, such as 't2.micro'.
        :param key_pair_name: The name of the key pair to use for SSH access.
        :param security_group_ids: A list of security group IDs to associate with the instance.
                                   If not specified, the default security group of the VPC is used.
        :return: A list of dictionaries representing Boto3 Instance objects representing the newly created instances.
        """
        try:
            instance_params = {
                "ImageId": image_id,
                "InstanceType": instance_type,
                "KeyName": key_pair_name,
            }
            if security_group_ids is not None:
                instance_params["SecurityGroupIds"] = security_group_ids

            response = self.ec2_client.run_instances(
                **instance_params, MinCount=1, MaxCount=1, Monitoring={'Enabled': True},
            )

            instance = response["Instances"][0]

            # Add a tag to the instance with the specified name
            if instance_name:
                self.add_tag(instance["InstanceId"], "Name", instance_name)

            self.instances.append(instance)
            waiter = self.ec2_client.get_waiter("instance_running")
            waiter.wait(InstanceIds=[instance["InstanceId"]])
        except ClientError as err:
            params_str = "\n\t".join(
                f"{key}: {value}" for key, value in instance_params.items()
            )
            logger.error(
                f"Failed to complete instance creation request.\nRequest details:{params_str}"
            )
            error_code = err.response["Error"]["Code"]
            if error_code == "InstanceLimitExceeded":
                logger.error(
                    (
                        f"Insufficient capacity for instance type '{instance_type}'. "
                        "Terminate unused instances or contact AWS Support for a limit increase."
                    )
                )
            if error_code == "InsufficientInstanceCapacity":
                logger.error(
                    (
                        f"Insufficient capacity for instance type '{instance_type}'. "
                        "Select a different instance type or launch in a different availability zone."
                    )
                )
            raise
        return self.instances
      
    def exists(self, instance_name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves an instance with a specified name.

        :param instance_name: The name of the instance to retrieve.
        :return: The instance with the specified name, or None if no instance is found.
        """
        response = self.ec2_client.describe_instances(
            Filters=[{"Name": "tag:Name", "Values": [instance_name]}]
        )

        # If an instance with the same name already exists, return the existing instance
        if response["Reservations"]:
            state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
            # Check if the state of the instance is 'running'
            if state in ["running", "stopping", "stopped"]:          
                print("Found instance with name ", instance_name)
                instance = response["Reservations"][0]["Instances"][0]
                return instance
            elif state in ["shutting-down", "terminated"]:
                print("Found instance with name ", instance_name, " but it is in a terminated or shutting-down state. Removing the tag.")
                self.remove_tag(response["Reservations"][0]["Instances"][0]["InstanceId"], "Name")

        return None
    
    def retrieve(self, instance_name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves an instance with a specified name.

        :param instance_name: The name of the instance to retrieve.
        :return: The instance with the specified name, or None if no instance is found.
        """
        response = self.ec2_client.describe_instances(
            Filters=[{"Name": "tag:Name", "Values": [instance_name]}]
        )

        # If an instance with the same name already exists, return the existing instance
        if response["Reservations"]:
            state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
            # Check if the state of the instance is 'running'
            if state == "running":            
                print("Registering the instance with name ", instance_name)
                instance = response["Reservations"][0]["Instances"][0]
                self.instances.append(instance)
                waiter = self.ec2_client.get_waiter("instance_running")
                waiter.wait(InstanceIds=[instance["InstanceId"]])
                return instance
            elif state in ["stopping", "stopped"]:
                print("Registering instance with name ", instance_name, ". Re-Starting the instance.")
                # Start the instance
                self.ec2_client.start_instances(InstanceIds=[response["Reservations"][0]["Instances"][0]["InstanceId"]])
                instance = response["Reservations"][0]["Instances"][0]
                self.instances.append(instance)
                waiter = self.ec2_client.get_waiter("instance_running")
                waiter.wait(InstanceIds=[instance["InstanceId"]])
                return instance
            elif state in ["terminated", "shutting-down"]:
                print("Found instance with name ", instance_name, " but it is in a terminated or shutting-down state. Removing the tag.")
                self.remove_tag(response["Reservations"][0]["Instances"][0]["InstanceId"], "Name")

        return None

    def add_tag(self, instance_id: str, key: str, value: str) -> None:
        """
        Adds a tag to an instance.

        :param instance_id: The ID of the instance to tag.
        :param key: The key of the tag.
        :param value: The value of the tag.
        """
        try:
            self.ec2_client.create_tags(Resources=[instance_id], Tags=[{"Key": key, "Value": value}])
        except ClientError as err:
            logger.error(f"Failed to tag instance {instance_id} with key '{key}' and value '{value}'.")
            raise
    
    def remove_tag(self, instance_id: str, key: str) -> None:
        """
        Removes a tag from an instance.

        :param instance_id: The ID of the instance to untag.
        :param key: The key of the tag to remove.
        """
        try:
            self.ec2_client.delete_tags(Resources=[instance_id], Tags=[{"Key": key}])
        except ClientError as err:
            logger.error(f"Failed to remove tag '{key}' from instance {instance_id}.")
            raise
    
    def display(self, state_filter: Optional[str] = "running") -> None:
        """
        Displays information about instances, filtering by the specified state.

        :param state_filter: The instance state to include in the output. Only instances in this state
                             will be displayed. Default is 'running'. Example states: 'running', 'stopped'.
        """
        if not self.instances:
            logger.info("No instances to display.")
            return

        instance_ids = [instance["InstanceId"] for instance in self.instances]
        paginator = self.ec2_client.get_paginator("describe_instances")
        page_iterator = paginator.paginate(InstanceIds=instance_ids)

        try:
            for page in page_iterator:
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        instance_state = instance["State"]["Name"]

                        # Apply the state filter (default is 'running')
                        if state_filter and instance_state != state_filter:
                            continue  # Skip this instance if it doesn't match the filter

                        # Create a formatted string with instance details
                        instance_info = (
                            f"• ID: {instance['InstanceId']}\n"
                            f"• Image ID: {instance['ImageId']}\n"
                            f"• Instance type: {instance['InstanceType']}\n"
                            f"• Key name: {instance['KeyName']}\n"
                            f"• VPC ID: {instance['VpcId']}\n"
                            f"• Public IP: {instance.get('PublicIpAddress', 'N/A')}\n"
                            f"• State: {instance_state}"
                        )
                        print(instance_info)

        except ClientError as err:
            logger.error(
                f"Failed to display instance(s). : {' '.join(map(str, instance_ids))}"
            )
            error_code = err.response["Error"]["Code"]
            if error_code == "InvalidInstanceID.NotFound":
                logger.error(
                    "One or more instance IDs do not exist. "
                    "Please verify the instance IDs and try again."
                )
                raise

    def terminate(self) -> None:
        """
        Terminates instances and waits for them to reach the terminated state.
        """
        if not self.instances:
            logger.info("No instances to terminate.")
            return

        instance_ids = [instance["InstanceId"] for instance in self.instances]
        try:
            self.ec2_client.terminate_instances(InstanceIds=instance_ids)
            waiter = self.ec2_client.get_waiter("instance_terminated")
            waiter.wait(InstanceIds=instance_ids)
            self.instances.clear()
            for instance_id in instance_ids:
                print(f"• Instance ID: {instance_id}\n" f"• Action: Terminated")

        except ClientError as err:
            logger.error(
                f"Failed instance termination details:\n\t{str(self.instances)}"
            )
            error_code = err.response["Error"]["Code"]
            if error_code == "InvalidInstanceID.NotFound":
                logger.error(
                    "One or more instance IDs do not exist. "
                    "Please verify the instance IDs and try again."
                )
            raise

    def start(self) -> Optional[Dict[str, Any]]:
        """
        Starts instances and waits for them to be in a running state.

        :return: The response to the start request.
        """
        if not self.instances:
            logger.info("No instances to start.")
            return None

        instance_ids = [instance["InstanceId"] for instance in self.instances]
        try:
            start_response = self.ec2_client.start_instances(InstanceIds=instance_ids)
            waiter = self.ec2_client.get_waiter("instance_running")
            waiter.wait(InstanceIds=instance_ids)
            return start_response
        except ClientError as err:
            logger.error(
                f"Failed to start instance(s): {','.join(map(str, instance_ids))}"
            )
            error_code = err.response["Error"]["Code"]
            if error_code == "IncorrectInstanceState":
                logger.error(
                    "Couldn't start instance(s) because they are in an incorrect state. "
                    "Ensure the instances are in a stopped state before starting them."
                )
            raise

    def stop(self) -> Optional[Dict[str, Any]]:
        """
        Stops instances and waits for them to be in a stopped state.

        :return: The response to the stop request, or None if there are no instances to stop.
        """
        if not self.instances:
            logger.info("No instances to stop.")
            return None

        instance_ids = [instance["InstanceId"] for instance in self.instances]
        try:
            # Attempt to stop the instances
            stop_response = self.ec2_client.stop_instances(InstanceIds=instance_ids)
            waiter = self.ec2_client.get_waiter("instance_stopped")
            waiter.wait(InstanceIds=instance_ids)
        except ClientError as err:
            logger.error(
                f"Failed to stop instance(s): {','.join(map(str, instance_ids))}"
            )
            error_code = err.response["Error"]["Code"]
            if error_code == "IncorrectInstanceState":
                logger.error(
                    "Couldn't stop instance(s) because they are in an incorrect state. "
                    "Ensure the instances are in a running state before stopping them."
                )
            raise
        return stop_response

    def get_images(self, image_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Gets information about Amazon Machine Images (AMIs) from a list of AMI IDs.

        :param image_ids: The list of AMI IDs to look up.
        :return: A list of dictionaries representing the requested AMIs.
        """
        try:
            response = self.ec2_client.describe_images(ImageIds=image_ids)
            images = response["Images"]
        except ClientError as err:
            logger.error(f"Failed to stop AMI(s): {','.join(map(str, image_ids))}")
            error_code = err.response["Error"]["Code"]
            if error_code == "InvalidAMIID.NotFound":
                logger.error("One or more of the AMI IDs does not exist.")
            raise
        return images

    def get_instance_types(
        self, architecture: str = "x86_64", sizes: List[str] = ["*.micro", "*.small", "*.medium", "*.large"]
    ) -> List[Dict[str, Any]]:
        """
        Gets instance types that support the specified architecture and size.
        See https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstanceTypes.html
        for a list of allowable parameters.

        :param architecture: The architecture supported by instance types. Default: 'x86_64'.
        :param sizes: The size of instance types. Default: '*.micro', '*.small',
        :return: A list of dictionaries representing instance types that support the specified architecture and size.
        """
        try:
            inst_types = []
            paginator = self.ec2_client.get_paginator("describe_instance_types")
            for page in paginator.paginate(
                Filters=[
                    {
                        "Name": "processor-info.supported-architecture",
                        "Values": [architecture],
                    },
                    {"Name": "instance-type", "Values": sizes},
                ]
            ):
                inst_types += page["InstanceTypes"]
        except ClientError as err:
            logger.error(
                f"Failed to get instance types: {architecture}, {','.join(map(str, sizes))}"
            )
            error_code = err.response["Error"]["Code"]
            if error_code == "InvalidParameterValue":
                logger.error(
                    "Parameters are invalid. "
                    "Ensure architecture and size strings conform to DescribeInstanceTypes API reference."
                )
            raise
        else:
            return inst_types