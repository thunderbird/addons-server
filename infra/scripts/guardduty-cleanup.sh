#!/usr/bin/env bash
# guardduty-cleanup.sh
#
# Cleans up GuardDuty-provisioned artefacts that block VPC deletion after
# `pulumi destroy`. When GuardDuty is enabled and a VPC exists, AWS
# automatically creates:
#   - A VPC endpoint (com.amazonaws.guardduty-data)
#   - ENIs attached to that endpoint
#   - A security group for the endpoint
#
# These are NOT managed by Pulumi and thus not removed by `pulumi destroy`,
# which causes the VPC deletion to fail.
#
# Basic usage:
#   ./guardduty-cleanup.sh <vpc-id> [--dry-run] [--force]
#
# Examples:
#   ./guardduty-cleanup.sh vpc-02ed42011af62798d --dry-run   # preview only
#   ./guardduty-cleanup.sh vpc-02ed42011af62798d             # delete (with tag check)
#   ./guardduty-cleanup.sh vpc-02ed42011af62798d --force     # skip tag check
#
# Safe to run in a SECOND TERMINAL while `pulumi destroy` is retrying the
# VPC deletion. The GuardDuty resources are not in Pulumi state, so removing
# them out-of-band is safe -- Pulumi's next retry will find the VPC clean
# and delete it successfully.
#
# Safety:
#   - Only targets GuardDuty VPC endpoints (matched by service name)
#   - Only deletes ENIs and SGs that belong to those specific endpoints
#   - Refuses to operate unless VPC has pulumi_project=thunderbird-addons tag
#     (override with --force)
#   - Idempotent: safe to run multiple times
#   - Use --dry-run first to preview what would be deleted
#
# Prerequisites:
#   - AWS CLI v2 configured with appropriate credentials
#   - jq installed

set -euo pipefail

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
VPC_ID="${1:-}"
DRY_RUN=false
FORCE=false

if [[ -z "$VPC_ID" ]]; then
    echo "Usage: $0 <vpc-id> [--dry-run] [--force]"
    echo ""
    echo "Options:"
    echo "  --dry-run   Preview what would be deleted without making changes"
    echo "  --force     Skip VPC tag safety check"
    exit 1
fi

shift
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --force)   FORCE=true ;;
        *)         echo "Unknown option: $arg"; exit 1 ;;
    esac
done

REGION="${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}"
echo "Region:  $REGION"
echo "VPC:     $VPC_ID"
echo "Dry run: $DRY_RUN"
echo ""

# ---------------------------------------------------------------------------
# Safety check: verify VPC has expected tags
# ---------------------------------------------------------------------------
if [[ "$FORCE" == false ]]; then
    echo "=== Safety Check: VPC Tags ==="
    VPC_PROJECT_TAG=$(aws ec2 describe-vpcs \
        --region "$REGION" \
        --vpc-ids "$VPC_ID" \
        --query 'Vpcs[0].Tags[?Key==`pulumi_project`].Value | [0]' \
        --output text 2>/dev/null || echo "None")

    if [[ "$VPC_PROJECT_TAG" != "thunderbird-addons" ]]; then
        echo "  ERROR: VPC $VPC_ID does not have tag pulumi_project=thunderbird-addons"
        echo "  Found: pulumi_project=$VPC_PROJECT_TAG"
        echo ""
        echo "  This safety check should prevent accidental cleanup of the wrong VPC"
        echo "  Use --force to override if you are certain this is correct"
        exit 1
    fi
    echo "  VPC tag check passed (pulumi_project=thunderbird-addons)"
    echo ""
fi

# ---------------------------------------------------------------------------
# Step 1: Find GuardDuty VPC endpoints and collect their ENI/SG metadata
# ---------------------------------------------------------------------------
echo "=== Step 1: Discover GuardDuty VPC Endpoints ==="

GUARDDUTY_SERVICE="com.amazonaws.${REGION}.guardduty-data"

ENDPOINT_JSON=$(aws ec2 describe-vpc-endpoints \
    --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=service-name,Values=$GUARDDUTY_SERVICE" \
    --output json \
    --query 'VpcEndpoints' 2>/dev/null || echo "[]")

ENDPOINT_COUNT=$(echo "$ENDPOINT_JSON" | jq 'length')

if [[ "$ENDPOINT_COUNT" -eq 0 ]]; then
    echo "  No GuardDuty VPC endpoints found. Nothing to clean up."
    exit 0
fi

# Extract the endpoint IDs, their ENI IDs, and their SG IDs
ENDPOINT_IDS=$(echo "$ENDPOINT_JSON" | jq -r '.[].VpcEndpointId')
ENI_IDS=$(echo "$ENDPOINT_JSON" | jq -r '.[].NetworkInterfaceIds[]' 2>/dev/null | sort -u || true)
SG_IDS=$(echo "$ENDPOINT_JSON" | jq -r '.[].Groups[].GroupId' 2>/dev/null | sort -u || true)

echo "  Found $ENDPOINT_COUNT GuardDuty endpoint(s):"
for eid in $ENDPOINT_IDS; do echo "    - $eid"; done
echo ""
if [[ -n "$ENI_IDS" ]]; then
    echo "  Associated ENIs:"
    for eni in $ENI_IDS; do echo "    - $eni"; done
    echo ""
fi
if [[ -n "$SG_IDS" ]]; then
    echo "  Associated SGs:"
    for sg in $SG_IDS; do echo "    - $sg"; done
    echo ""
fi

# ---------------------------------------------------------------------------
# Step 2: Delete the GuardDuty VPC endpoints
# ---------------------------------------------------------------------------
echo "=== Step 2: Delete GuardDuty VPC Endpoints ==="

for ENDPOINT_ID in $ENDPOINT_IDS; do
    if [[ "$DRY_RUN" == false ]]; then
        echo "  Deleting endpoint $ENDPOINT_ID..."
        aws ec2 delete-vpc-endpoints \
            --region "$REGION" \
            --vpc-endpoint-ids "$ENDPOINT_ID"
        echo "  Deleted."
    else
        echo "  [DRY RUN] Would delete endpoint $ENDPOINT_ID"
    fi
done

# Wait for ENIs to release with retry backoff (can take 15-60s)
if [[ "$DRY_RUN" == false && -n "$ENI_IDS" ]]; then
    MAX_RETRIES=4
    WAIT_SECS=15
    for ATTEMPT in $(seq 1 $MAX_RETRIES); do
        echo "  Waiting ${WAIT_SECS}s for ENI release (attempt ${ATTEMPT}/${MAX_RETRIES})..."
        sleep "$WAIT_SECS"

        ALL_CLEAR=true
        for ENI_ID in $ENI_IDS; do
            ENI_STATUS=$(aws ec2 describe-network-interfaces \
                --region "$REGION" \
                --network-interface-ids "$ENI_ID" \
                --query 'NetworkInterfaces[0].Status' \
                --output text 2>/dev/null || echo "not-found")
            if [[ "$ENI_STATUS" == "in-use" ]]; then
                ALL_CLEAR=false
                break
            fi
        done

        if [[ "$ALL_CLEAR" == true ]]; then
            echo "  All ENIs released."
            break
        fi

        if [[ "$ATTEMPT" -eq "$MAX_RETRIES" ]]; then
            echo "  Some ENIs still in-use after ${MAX_RETRIES} attempts. Proceeding with best effort"
        fi
        WAIT_SECS=$((WAIT_SECS + 10))
    done
fi
echo ""

# ---------------------------------------------------------------------------
# Step 3: Delete endpoint-linked ENIs (if still present after endpoint delete)
# ---------------------------------------------------------------------------
echo "=== Step 3: Clean Up Endpoint ENIs ==="

if [[ -z "$ENI_IDS" ]]; then
    echo "  No endpoint ENIs to clean up."
else
    for ENI_ID in $ENI_IDS; do
        ENI_STATUS=$(aws ec2 describe-network-interfaces \
            --region "$REGION" \
            --network-interface-ids "$ENI_ID" \
            --query 'NetworkInterfaces[0].Status' \
            --output text 2>/dev/null || echo "not-found")

        if [[ "$ENI_STATUS" == "not-found" ]]; then
            echo "  ENI $ENI_ID already gone (released with endpoint)."
            continue
        fi

        if [[ "$ENI_STATUS" == "available" ]]; then
            if [[ "$DRY_RUN" == false ]]; then
                echo "  Deleting ENI $ENI_ID..."
                aws ec2 delete-network-interface \
                    --region "$REGION" \
                    --network-interface-id "$ENI_ID"
                echo "  Deleted."
            else
                echo "  [DRY RUN] Would delete ENI $ENI_ID (status: $ENI_STATUS)"
            fi
        else
            echo "  ENI $ENI_ID still in '$ENI_STATUS' state -- skipping"
        fi
    done
fi
echo ""

# ---------------------------------------------------------------------------
# Step 4: Delete endpoint-linked SGs (if not the VPC default SG)
# ---------------------------------------------------------------------------
echo "=== Step 4: Clean Up Endpoint Security Groups ==="

if [[ -z "$SG_IDS" ]]; then
    echo "  No endpoint SGs to clean up."
else
    # Get the default SG for this VPC (cannot be deleted)
    DEFAULT_SG=$(aws ec2 describe-security-groups \
        --region "$REGION" \
        --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=default" \
        --query 'SecurityGroups[0].GroupId' \
        --output text 2>/dev/null || echo "")

    for SG_ID in $SG_IDS; do
        if [[ "$SG_ID" == "$DEFAULT_SG" ]]; then
            echo "  SG $SG_ID is the VPC default SG -- skipping."
            continue
        fi

        if [[ "$DRY_RUN" == false ]]; then
            echo "  Deleting SG $SG_ID..."
            aws ec2 delete-security-group \
                --region "$REGION" \
                --group-id "$SG_ID" 2>/dev/null \
                && echo "  Deleted." \
                || echo "  Could not delete (may still be referenced by an ENI; retry after ENI cleanup)."
        else
            echo "  [DRY RUN] Would delete SG $SG_ID"
        fi
    done
fi
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=== Done ==="
if [[ "$DRY_RUN" == true ]]; then
    echo "Dry run complete. Re-run without --dry-run to apply changes."
else
    echo "Cleanup complete. Pulumi's next VPC deletion retry should succeed."
    echo ""
    echo "If VPC deletion still fails, check for:"
    echo "  - ENIs still in 'in-use' state (wait a few seconds and re-run)"
    echo "  - Other non-Pulumi resources in the VPC:"
    echo "    aws ec2 describe-network-interfaces --filters Name=vpc-id,Values=$VPC_ID"
fi
