PAYLOAD_DIR ?= payload_good2-1.1.0
PAYLOAD_NAME := $(notdir $(PAYLOAD_DIR))
SOFTWARE_VERSION := $(lastword $(subst -, ,$(PAYLOAD_NAME)))
PAYLOAD_TAR ?= $(PAYLOAD_DIR).tar
ARTIFACT ?= $(PAYLOAD_DIR).mender
ARTIFACT_NAME ?= $(PAYLOAD_NAME)
DEVICE_TYPE ?= moducop-cpu01
SOFTWARE_NAME ?= os-customization
SOFTWARE_FILESYSTEM ?= data-partition
PAYLOAD_TYPE ?= os-customization

ifeq ($(SOFTWARE_VERSION),$(PAYLOAD_NAME))
$(error PAYLOAD_DIR must end in -<version>, for example payload_good2-1.1.0)
endif

.PHONY: all print-config

all: $(ARTIFACT)

print-config:
	@printf '%s\n' "payload=$(PAYLOAD_DIR) version=$(SOFTWARE_VERSION)"

# The Update Module accepts one archive whose root contains manifest.json and
# etc/, so archive the payload directory's contents rather than the directory.
$(PAYLOAD_TAR): $(PAYLOAD_DIR)/manifest.json
	tar -C $(PAYLOAD_DIR) -cf $@ .

$(ARTIFACT): $(PAYLOAD_TAR)
	mender-artifact write module-image \
		-n $(ARTIFACT_NAME) \
		-t $(DEVICE_TYPE) \
		-T $(PAYLOAD_TYPE) \
		--software-filesystem $(SOFTWARE_FILESYSTEM) \
		--software-name $(SOFTWARE_NAME) \
		--software-version $(SOFTWARE_VERSION) \
		-f $(PAYLOAD_TAR) \
		-o $@
