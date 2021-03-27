CREATE TABLE IF NOT EXISTS public.users (
	user_id int8 NOT NULL,
	key_salt text NOT NULL,
	is_mod bool NOT NULL DEFAULT false,
	is_banned bool NOT NULL DEFAULT false,
	CONSTRAINT users_pk PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS  public.pixel_history (
	pixel_history_id serial NOT NULL,
	created_at TIMESTAMP NOT NULL DEFAULT now(),
	x int2 NOT NULL,
	y int2 NOT NULL,
	rgb varchar(6) NOT NULL,
	user_id int8 NOT NULL,
	deleted bool NOT NULL,
	CONSTRAINT pixel_history_pk PRIMARY KEY (pixel_history_id)
);

CREATE TABLE IF NOT EXISTS public.rate_limits (
    request_id serial NOT NULL,
    user_id int8,
    route varchar(255) NOT NULL,
    expiration TIMESTAMP NOT NULL,
    CONSTRAINT rate_limits_pk PRIMARY KEY (request_id)
);

CREATE TABLE IF NOT EXISTS public.cooldowns (
    request_id serial NOT NULL,
    user_id int8,
    route varchar(255) NOT NULL,
    expiration TIMESTAMP NOT NULL,
    CONSTRAINT cooldowns_pk PRIMARY KEY (request_id)
);

ALTER TABLE public.pixel_history ADD CONSTRAINT pixel_history_fk FOREIGN KEY (user_id) REFERENCES users(user_id);
ALTER TABLE public.rate_limits ADD CONSTRAINT rate_limits_fk FOREIGN KEY (user_id) REFERENCES users(user_id);
ALTER TABLE public.cooldowns ADD CONSTRAINT cooldowns_fk FOREIGN KEY (user_id) REFERENCES users(user_id);

CREATE OR REPLACE VIEW public.current_pixel
AS SELECT PH.x,
          PH.y,
          PH.pixel_history_id,
          PH.rgb
FROM (
    SELECT MAX(pixel_history_id) as pixel_history_id
    FROM pixel_history
    WHERE NOT deleted
    GROUP BY x, y
) most_recent_pixels
INNER JOIN pixel_history PH USING (pixel_history_id)
