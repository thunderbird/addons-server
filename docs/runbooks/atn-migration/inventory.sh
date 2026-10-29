#!/bin/sh
# usage: inventory.sh <label>   -> /work/<label>/{counts.tsv,columns.tsv,create/*.sql,schema_version}
set -u
L=/work/$1; mkdir -p $L/create
M="mysql --batch --skip-column-names -h$DB_HOST -u$DB_USER $DB_NAME"
$M -e "SELECT version FROM schema_version" > $L/schema_version 2>&1
: > $L/counts.tsv
while read t; do
  e=$($M -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='$t'")
  if [ "$e" = 1 ]; then
    s=$(date +%s.%N); n=$($M -e "SELECT COUNT(*) FROM \`$t\`"); d=$(echo "$(date +%s.%N) - $s" | bc 2>/dev/null || echo na)
    sz=$($M -e "SELECT ROUND((data_length+index_length)/1048576,1) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='$t'")
    printf '%s\tpresent\t%s\t%s MiB\t%ss\n' "$t" "$n" "$sz" "$d" >> $L/counts.tsv
    $M -e "SHOW CREATE TABLE \`$t\`\G" > $L/create/$t.sql
  else
    printf '%s\tabsent\t-\t-\t-\n' "$t" >> $L/counts.tsv
  fi
done < /kit/tables.txt
$M -e "SELECT table_name, column_name, column_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema=DATABASE() AND ((table_name='addons' AND column_name IN ('locale_disambiguation','contributions','externalsoftware','auto_repackage','requires_sensitive_data_access')) OR (table_name='collections' AND column_name IN ('icontype','uuid','rating','downvotes','upvotes','monthly_subscribers','downloads','subscribers','weekly_subscribers')) OR (table_name='users' AND column_name='display_collections_fav') OR (table_name='versions' AND column_name='source_git_hash') OR (table_name='hubrsskeys' AND column_name='rsskey') OR (table_name='version_previews' AND column_name='colors') OR (table_name='discovery_discoveryitem' AND column_name='position_override') OR (table_name='addons_addonreviewerflags' AND column_name='needs_sensitive_data_access_review') OR (table_name='editors_autoapprovalsummary' AND column_name='has_sensitive_data_access') OR (table_name='abuse_reports' AND column_name IN ('client_id','country_code','ip_address','addon_install_entry_point','report_entry_point')) OR (table_name='categories' AND column_name='name')) ORDER BY 1,2" > $L/columns.tsv
$M -e "SELECT name FROM waffle_switch WHERE name IN ('content-optimization','akismet-rating-action','akismet-addon-action','akismet-collection-action','boost-webextensions-in-search','activate-basket-sync','2fa-for-developers','return-to-amo','disallow-legacy-submissions') UNION ALL SELECT CONCAT('sample:',name) FROM waffle_sample WHERE name='activate-autograph-file-signing'" > $L/waffles.tsv
$M -e "SELECT id, application_id, addontype_id, slug FROM categories WHERE id BETWEEN 400 AND 428 OR application_id=61" > $L/categories_400.tsv
$M -e "SELECT constraint_name, table_name, referenced_table_name FROM information_schema.referential_constraints WHERE constraint_schema=DATABASE() AND (table_name IN ('categories','webext_permission_descriptions_translation_id','addons_dependencies','addons_addonfeaturecompatibility','compatibility_reports','compat_totals','collection_subscriptions','collections_votes','stats_addons_collections_counts','stats_collections_counts','stats_collections') OR referenced_table_name IN ('addons_dependencies','addons_addonfeaturecompatibility','compatibility_reports','compat_totals','collection_subscriptions','collections_votes','stats_collections','webext_permission_descriptions_translation_id'))" > $L/fks.tsv
echo "inventory $1 done"; cat $L/schema_version
