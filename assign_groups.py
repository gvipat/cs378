# coding: utf-8
import polars
canvas = polars.read_csv('Assignment Groups.csv')
aws = polars.read_csv('roster.csv')
joined = canvas.join(aws, left_on='user_id', right_on='student_id').drop('group_name').rename({'group_id':'group_name'})
joined.write_csv('groups.csv')