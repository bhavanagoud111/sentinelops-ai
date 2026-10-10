variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "client_security_group_id" {
  type = string
}

variable "kafka_version" {
  type    = string
  default = "3.9.x"
}

variable "instance_type" {
  type    = string
  default = "kafka.t3.small"
}

variable "tags" {
  type    = map(string)
  default = {}
}
