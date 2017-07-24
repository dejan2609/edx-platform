from contentstore.course_group_config import GroupConfiguration
from contentstore.models import MigrateVerifiedTrackCohortsSetting
from django.conf import settings
from course_modes.models import CourseMode
from django.core.management.base import BaseCommand, CommandError

from openedx.core.djangoapps.course_groups.cohorts import CourseCohort
from openedx.core.djangoapps.course_groups.models import (CourseUserGroup, CourseUserGroupPartitionGroup)
from openedx.core.djangoapps.verified_track_content.models import VerifiedTrackCohortedCourse

from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.django import modulestore
from xmodule.partitions.partitions import ENROLLMENT_TRACK_PARTITION_ID
from xmodule.partitions.partitions_service import PartitionService


class Command(BaseCommand):
    """
    Migrates a course's xblock's group_access from Verified Track Cohorts to Enrollment Tracks
    """

    def handle(self, *args, **options):

        errors = []

        module_store = modulestore()

        verified_track_cohorts_settings = self._enabled_settings()

        for verified_track_cohorts_setting in verified_track_cohorts_settings:
            old_course_key = verified_track_cohorts_setting.old_course_key
            rerun_course_key = verified_track_cohorts_setting.rerun_course_key
            audit_cohort_names = verified_track_cohorts_setting.get_audit_cohort_names()

            # Get the CourseUserGroup IDs for the audit course names from the old course
            audit_course_user_group_ids = CourseUserGroup.objects.filter(
                name__in=audit_cohort_names,
                course_id=old_course_key,
                group_type=CourseUserGroup.COHORT,
            ).values_list('id', flat=True)

            # Get all of the audit CourseCohorts from the above IDs that are RANDOM
            random_audit_course_user_group_ids = CourseCohort.objects.filter(
                course_user_group_id__in=audit_course_user_group_ids,
                assignment_type=CourseCohort.RANDOM
            ).values_list('course_user_group_id', flat=True)

            # Get the CourseUserGroupPartitionGroup for the above IDs, these contain the partition IDs and group IDs
            # that are set for group_access inside of modulestore
            random_audit_course_user_group_partition_groups = list(CourseUserGroupPartitionGroup.objects.filter(
                course_user_group_id__in=random_audit_course_user_group_ids
            ))

            if not random_audit_course_user_group_partition_groups:
                errors.append('No Audit Course Groups found with names: %s' % audit_cohort_names)

            # Get the single Verified Track Cohorted Course for the old course
            verified_track_cohorted_course = VerifiedTrackCohortedCourse.objects.get(course_key=old_course_key)

            # If there is no verified track, raise an error
            if not verified_track_cohorted_course:
                raise CommandError("No VerifiedTrackCohortedCourse found for course: '%s'" % rerun_course_key)

            # Get the single CourseUserGroupPartitionGroup for the verified_track
            # based on the verified_track name for the old course
            verified_course_user_group = CourseUserGroup.objects.get(
                course_id=old_course_key,
                group_type=CourseUserGroup.COHORT,
                name=verified_track_cohorted_course.verified_cohort_name
            )
            verified_course_user_group_partition_group = CourseUserGroupPartitionGroup.objects.get(
                course_user_group_id=verified_course_user_group.id
            )

            # Get the enrollment track CourseModes for the new course
            audit_course_mode = CourseMode.objects.get(
                course_id=rerun_course_key,
                mode_slug=CourseMode.AUDIT
            )
            verified_course_mode = CourseMode.objects.get(
                course_id=rerun_course_key,
                mode_slug=CourseMode.VERIFIED
            )
            # Verify that the enrollment track course modes exist
            if not audit_course_mode or not verified_course_mode:
                raise CommandError("Audit or Verified course modes are not defined for course: '%s'" % rerun_course_key)

            items = module_store.get_items(rerun_course_key)
            items_to_update = []
            if not items:
                raise CommandError("Items for Course with key '%s' not found." % rerun_course_key)

            for item in items:
                # Verify that there exists group access for this xblock, otherwise skip these checks
                if item.group_access:
                    set_audit_enrollment_track = False
                    set_verified_enrollment_track = False

                    # Check the partition and group IDs for the audit course groups, if they exist in
                    # the xblock's access settings then set the audit track flag to true
                    for audit_course_user_group_partition_group in random_audit_course_user_group_partition_groups:
                        audit_partition_group_access = item.group_access.get(
                            audit_course_user_group_partition_group.partition_id,
                            None
                        )
                        if (audit_partition_group_access
                                and audit_course_user_group_partition_group.group_id in audit_partition_group_access):
                            set_audit_enrollment_track = True

                    # Check the partition and group IDs for the verified course group, if it exists in
                    # the xblock's access settings then set the verified track flag to true
                    verified_partition_group_access = item.group_access.get(
                        verified_course_user_group_partition_group.partition_id,
                        None
                    )
                    if (verified_partition_group_access
                            and verified_course_user_group_partition_group.group_id in verified_partition_group_access):
                        set_verified_enrollment_track = True

                    # Add the enrollment track ids to a group access array
                    enrollment_track_group_access = []
                    if set_audit_enrollment_track:
                        enrollment_track_group_access.append(settings.COURSE_ENROLLMENT_MODES['audit'])
                    if set_verified_enrollment_track:
                        enrollment_track_group_access.append(settings.COURSE_ENROLLMENT_MODES['verified'])

                    # If either the audit track, or verified track needed an update, set the access, update and publish
                    if set_verified_enrollment_track or set_audit_enrollment_track:
                        # Sets whether or not an xblock has changes
                        has_changes = module_store.has_changes(item)

                        # Check that the xblock does not have changes and add it to be updated, otherwise add an error
                        if not has_changes:
                            item.group_access = {ENROLLMENT_TRACK_PARTITION_ID: enrollment_track_group_access}
                            items_to_update.append(item)
                        else:
                            errors.append("XBlock '%s' with location '%s' needs access changes, but is a draft"
                                          % (item.display_name, item.location))

            partitions_to_delete = random_audit_course_user_group_partition_groups
            partitions_to_delete.append(verified_course_user_group_partition_group)

            # If there are no errors iterate over and update all of the items that had the access changed
            if not errors:
                for item in items_to_update:
                    module_store.update_item(item, ModuleStoreEnum.UserID.mgmt_command)
                    module_store.publish(item.location, ModuleStoreEnum.UserID.mgmt_command)

            # Check if we should delete any partition groups if there are no errors.
            # If there are errors, none of the xblock items will have been updated,
            # so this section will throw errors for each partition in use
            if partitions_to_delete and not errors:
                partition_service = PartitionService(rerun_course_key)
                course = partition_service.get_course()
                for partition_to_delete in partitions_to_delete:
                    # Get the user partition, and the index of that partition in the course
                    partition = partition_service.get_user_partition(partition_to_delete.partition_id)
                    partition_index = course.user_partitions.index(partition)
                    group_id = int(partition_to_delete.group_id)

                    # Sanity check to verify that all of the groups being deleted are empty,
                    # since they should have been converted to use enrollment tracks instead.
                    # Taken from contentstore/views/course.py.remove_content_or_experiment_group
                    usages = GroupConfiguration.get_partitions_usage_info(module_store, course)
                    used = group_id in usages
                    if used:
                        errors.append("Content group '%s' is in use and cannot be deleted."
                                      % partition_to_delete.group_id)

                    # If there are not errors, proceed to update the course and user_partitions
                    if not errors:
                        # Remove the groups that match the group ID of the partition to be deleted
                        # Else if there are no match groups left, remove the user partition
                        matching_groups = [group for group in partition.groups if group.id == group_id]
                        if matching_groups:
                            group_index = partition.groups.index(matching_groups[0])
                            partition.groups.pop(group_index)
                            # Update the course user partition with the updated groups
                            course.user_partitions[partition_index] = partition
                        else:
                            course.user_partitions.pop(partition_index)
                        module_store.update_item(course, ModuleStoreEnum.UserID.mgmt_command)

            # If there are any errors, join them together and raise the CommandError
            if errors:
                raise CommandError("\n".join(errors))

    def _enabled_settings(self):
        """
        Return the enabled entries for the MigrateVerifiedTrackCohortsSetting
        """
        return MigrateVerifiedTrackCohortsSetting.objects.filter(enabled=True)
