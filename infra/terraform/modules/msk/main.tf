# Optional managed Kafka (provisioned MSK). The services speak plain Kafka without SASL,
# so in-VPC plaintext is allowed alongside TLS; access is limited to the EKS security
# group. MSK Serverless is not used because it requires IAM SASL auth.
resource "aws_security_group" "this" {
  name        = "${var.name}-kafka"
  description = "Kafka from EKS only"
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "from_eks" {
  for_each                     = toset(["9092", "9094"])
  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
}

resource "aws_msk_cluster" "this" {
  cluster_name           = var.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = length(var.private_subnet_ids)

  broker_node_group_info {
    instance_type   = var.instance_type
    client_subnets  = var.private_subnet_ids
    security_groups = [aws_security_group.this.id]
    storage_info {
      ebs_storage_info {
        volume_size = 20
      }
    }
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS_PLAINTEXT"
      in_cluster    = true
    }
  }

  tags = var.tags
}
