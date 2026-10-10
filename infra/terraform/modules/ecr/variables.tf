variable "prefix" {
  type = string
}

variable "repositories" {
  type    = list(string)
  default = ["platform", "dashboard"]
}

variable "keep_images" {
  type    = number
  default = 20
}

# true lets `terraform destroy` remove repositories that still hold images (demo teardown).
variable "force_delete" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
