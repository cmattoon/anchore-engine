import json
import os
import hashlib
import time

import anchore_engine.configuration.localconfig
from anchore_engine import db
from anchore_engine.db import db_archivedocument
from anchore_engine.subsys import logger

use_db = False
data_volume = None
archive_initialized = False
archive_driver = 'db'

def initialize():
    global archive_initialized, data_volume, use_db, archive_driver

    localconfig = anchore_engine.configuration.localconfig.get_config()
    myconfig = localconfig['services']['catalog']

    try:
        data_volume = None
        if 'archive_data_dir' in myconfig:
            data_volume = myconfig['archive_data_dir']

        archive_driver = 'db'
        if 'archive_driver' in myconfig:
            archive_driver = myconfig['archive_driver']
        
        if 'use_db' in myconfig and myconfig['use_db']:
            archive_driver = 'db'

        # driver specific initializations here
        if archive_driver == 'db':
            use_db = True
        else:
            use_db = False
            initialize_archive_file(myconfig)

    except Exception as err:
        raise err

    logger.debug("archive initialization config: " + str([archive_driver, use_db, data_volume]))

    # this section is for conversion on initialization between db driver and other driver
    with db.session_scope() as dbsession:
        logger.debug("running archive driver converter")

        for archive_record in db_archivedocument.get_all_iter(session=dbsession):
            userId = archive_record['userId']
            bucket = archive_record['bucket']
            archiveid  = archive_record['archiveId']
            dataref = archive_record['documentName']
            if archive_record['jsondata']:
                try:
                    db_data = json.loads(archive_record['jsondata'])
                except:
                    logger.warn("could no load jsondata for archive record: " + str([userId, bucket, archiveid]))
                    db_data = None
            else:
                db_data = None

            if use_db and not db_data:
                try:
                    fs_data = read_archive_file(userId, bucket, archiveid, driver_override='localfs')
                except Exception as err:
                    logger.debug("no data: " + str(err))
                    fs_data = None
                if fs_data:
                    logger.debug("document data not in DB but is on FS - converting: " + str([userId, bucket, archiveid]))
                    with db.session_scope() as subdbsession:
                        db_archivedocument.add(userId, bucket, archiveid, archiveid+".json", {'jsondata': json.dumps(fs_data)}, session=subdbsession)
                    delete_archive_file(userId, bucket, archiveid, driver_override='localfs')

            elif not use_db and db_data:
                logger.debug("document data not on FS but is in DB - converting: " + str([userId, bucket, archiveid]))
                dataref = write_archive_file(userId, bucket, archiveid, db_data, driver_override='localfs')
                with db.session_scope() as subdbsession:
                    db_archivedocument.add(userId, bucket, archiveid, archiveid+".json", {'jsondata': "{}"}, session=subdbsession)

        logger.debug("archive driver converter complete")
    archive_initialized = True
    return(True)

def put_document(userId, bucket, archiveId, data):
    payload = {'document': data}
    return(put(userId, bucket, archiveId, payload))

def put(userId, bucket, archiveid, data):
    global archive_initialized, data_volume, use_db

    if not archive_initialized:
        raise Exception("archive not initialized")

    try:
        with db.session_scope() as dbsession:
            if use_db:
                dbdata = {'jsondata':json.dumps(data)}
            else:
                dbdata = {'jsondata': '{}', 'last_updated': int(time.time())}
                dataref = write_archive_file(userId, bucket, archiveid, data)
    
            db_archivedocument.add(userId, bucket, archiveid, archiveid+".json", dbdata, session=dbsession)
    except Exception as err:
        logger.debug("cannot put data: exception - " + str(err))
        raise err
    
    return(True)

def put_orig(userId, bucket, archiveid, data):
    global archive_initialized, data_volume, use_db

    if not archive_initialized:
        raise Exception("archive not initialized")

    if use_db:
        try:
            with db.session_scope() as dbsession:
                blarg = {'jsondata':json.dumps(data)}
                db_archivedocument.add(userId, bucket, archiveid, archiveid+".json", blarg, session=dbsession)
        except Exception as err:
            logger.debug("cannot put data: exception - " + str(err))
            raise err
    else:
        try:
            if not os.path.exists(os.path.join(data_volume, bucket)):
                os.makedirs(os.path.join(data_volume, bucket))

            with open(os.path.join(data_volume, bucket, archiveid+".json"), 'w') as OFH:
                OFH.write(json.dumps(data))

        except Exception as err:
            logger.debug("cannot put data: exception - " + str(err))
            raise err
    
    return(True)

def get_document_meta(userId, bucket, archiveId):
    with db.session_scope() as dbsession:
        ret = db_archivedocument.get_onlymeta(userId, bucket, archiveId, session=dbsession)
    return(ret)

def get_document(userId, bucket, archiveId):
    archive_document = get(userId, bucket, archiveId)
    ret = archive_document['document']
    return(ret)

def get(userId, bucket, archiveid):
    global archive_initialized, data_volume, use_db

    if not archive_initialized:
        raise Exception("archive not initialized")

    ret = {}

    try:
        with db.session_scope() as dbsession:
            result = db_archivedocument.get(userId, bucket, archiveid, session=dbsession)
        if result:
            if use_db:
                if 'jsondata' in result:
                    ret = json.loads(result['jsondata'])
                    del result
                else:
                    raise Exception("no archive record JSON data found in DB")
            else:
                ret = read_archive_file(userId, bucket, archiveid)

    except Exception as err:
        logger.debug("cannot get data: exception - " + str(err))
        raise err

    return(ret)

def get_orig(userId, bucket, archiveid):
    global archive_initialized, data_volume, use_db

    if not archive_initialized:
        raise Exception("archive not initialized")

    ret = {}

    if use_db:
        try:
            with db.session_scope() as dbsession:
                result = db_archivedocument.get(userId, bucket, archiveid, session=dbsession)
            if result and 'jsondata' in result:
                ret = json.loads(result['jsondata'])
                del result
            else:
                raise Exception("no archive record JSON data found in DB")
        except Exception as err:
            logger.debug("cannot get data: exception - " + str(err))
            raise err
    else:
        try:
            with open(os.path.join(data_volume, bucket, archiveid+".json"), 'r') as FH:
                ret = json.loads(FH.read())
        except Exception as err:
            logger.debug("cannot get data: exception - " + str(err))
            raise err

    return(ret)

def delete(userId, bucket, archiveid):
    global archive_initialized, data_volume, use_db

    if not archive_initialized:
        raise Exception("archive not initialized")

    try:
        with db.session_scope() as dbsession:
            rc = db_archivedocument.delete(userId, bucket, archiveid, session=dbsession)
            if not rc:
                raise Exception("failed to delete DB record")

            if not use_db:
                delete_archive_file(userId, bucket, archiveid)

    except Exception as err:
        raise err

    return(True)

def delete_orig(userId, bucket, archiveid):
    global archive_initialized, data_volume, use_db

    if not archive_initialized:
        raise Exception("archive not initialized")

    if use_db:
        try:
            with db.session_scope() as dbsession:
                rc = db_archivedocument.delete(userId, bucket, archiveid, session=dbsession)
                if not rc:
                    raise Exception("failed to delete")
        except Exception as err:
            raise err
    else:
        try:
            if os.path.exists(os.path.join(data_volume, bucket, archiveid+".json")):
                os.remove(os.path.join(data_volume, bucket, archiveid+".json"))
        except Exception as err:            
            raise err

    return(True)

####### driver storage implementations #######

def initialize_archive_file(myconfig):
    global data_volume, archive_driver

    if archive_driver == 'localfs':
        try:
            if 'archive_data_dir' in myconfig:
                data_volume = myconfig['archive_data_dir']

            if not data_volume:
                raise Exception("the localfs archive_driver requires archive_data_dir to be set in config.yaml")

            if not os.path.exists(data_volume):
                os.makedirs(data_volume)
        except Exception as err:
            raise Exception("catalog service use_db set to false but no archive_data_dir is set, or is unavailable - exception: " + str(err))
    elif archive_driver == 'db':
        pass
    else:
        pass

    return(True)

def get_archive_filepath(userId, bucket, archiveId):
    global data_volume, use_db
    ret = None
    if data_volume:
        archive_path = os.path.join(data_volume, hashlib.md5(userId).hexdigest(), bucket)
        archive_file = os.path.join(archive_path, hashlib.md5(archiveId).hexdigest() + ".json")
        try:
            if not use_db and not os.path.exists(archive_path):
                os.makedirs(archive_path)
        except Exception as err:
            logger.error("cannot create archive data directory - exception: " + str(err))
            raise err
        ret = archive_file
    return(ret)

def write_archive_file(userId, bucket, archiveid, data, driver_override=None):
    global archive_driver

    ret = "none"

    use_driver = archive_driver
    if driver_override:
        use_driver = driver_override

    if use_driver == 'localfs':
        archive_file = get_archive_filepath(userId, bucket, archiveid)
        with open(archive_file, 'w') as OFH:
            OFH.write(json.dumps(data))
        ret = "file://"+archive_file
    elif use_driver == 'db':
        ret = "db"
    else:
        raise Exception("unknown storage driver ("+str(archive_driver)+" defined in config.yaml")

    return(ret)

def read_archive_file(userId, bucket, archiveid, driver_override=None):
    global archive_driver

    data = None

    use_driver = archive_driver
    if driver_override:
        use_driver = driver_override

    if use_driver == 'localfs':
        archive_file = get_archive_filepath(userId, bucket, archiveid)
        if os.path.exists(archive_file):
            with open(archive_file, 'r') as FH:
                data = json.loads(FH.read())
        else:
            raise Exception("cannot locate archive file ("+str(archive_file)+")")
    elif use_driver == 'db':
        data = None
    else:
        raise Exception("unknown storage driver ("+str(archive_driver)+" defined in config.yaml")

    return(data)

def delete_archive_file(userId, bucket, archiveid, driver_override=None):
    global archive_driver

    use_driver = archive_driver
    if driver_override:
        use_driver = driver_override

    if use_driver == 'localfs':
        archive_file = get_archive_filepath(userId, bucket, archiveid)
        if os.path.exists(archive_file):
            try:
                os.remove(archive_file)
            except Exception as err:
                logger.error("could not delete archive file ("+str(archive_file)+") - exception: " + str(err))
    elif use_driver == 'db':
        pass
    else:
        raise Exception("unknown storage driver ("+str(archive_driver)+" defined in config.yaml")

    return(True)

