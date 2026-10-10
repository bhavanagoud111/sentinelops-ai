variable "name" {
  type = string
}

variable "cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "tags" {
  type    = map(string)
  default = {}
}
